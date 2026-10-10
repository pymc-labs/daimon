"""Transient owner connection failure recovers; sustained loss drains gracefully."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg import CannotConnectNowError
from daimon.adapters.discord.runtime import hold_worker_owner
from daimon.core.stores.worker_ownership import owner_is_alive
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker


async def test_transient_owner_connection_loss_does_not_cancel_worker(
    db_nullpool_engine: AsyncEngine,
) -> None:
    entered, stopped, ownership_lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owner_key = 718211
    owner_engine = MagicMock(spec=AsyncEngine)
    attempts = 0

    def connect() -> AsyncConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ConnectionRefusedError("database restarting")
        return db_nullpool_engine.connect()

    owner_engine.connect.side_effect = connect

    async def worker() -> None:
        async with hold_worker_owner(
            owner_engine,
            owner_key=owner_key,
            ownership_lost=ownership_lost,
            probe_interval_s=0.01,
            retry_delay_s=0.01,
        ):
            entered.set()
            await stopped.wait()

    task = asyncio.create_task(worker())
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    owner_pid = text("""
        SELECT pid FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND classid = 0
          AND objid = :key AND objsubid = 1
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with factory() as session:
            pid = await session.scalar(owner_pid, {"key": owner_key})
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        async with asyncio.timeout(5):
            while True:
                async with factory() as session:
                    replacement = await session.scalar(owner_pid, {"key": owner_key})
                if replacement is not None and replacement != pid:
                    break
                await asyncio.sleep(0.01)
        assert not task.done(), "a transient DB failure must not cancel the adapter"
        assert not ownership_lost.is_set()
        assert attempts >= 3, "a refused reconnect must retry before reacquiring the same key"
        stopped.set()
        await asyncio.wait_for(task, timeout=5)
        async with factory() as session:
            assert not await owner_is_alive(session, owner_key)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "reconnect_error", [SQLAlchemyError, ConnectionRefusedError, CannotConnectNowError]
)
async def test_sustained_owner_loss_retries_then_drains_without_cancellation(
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    reconnect_error: type[Exception],
) -> None:
    from daimon.adapters.discord.__main__ import drain_on_owner_loss

    ownership_lost, stopped = asyncio.Event(), asyncio.Event()
    bot = MagicMock()
    bot.runtime.ownership_lost = ownership_lost
    bot._drain_and_close = AsyncMock(side_effect=lambda: stopped.set())
    watcher = asyncio.create_task(drain_on_owner_loss(bot))
    try:
        async with hold_worker_owner(
            db_nullpool_engine,
            owner_key=718212,
            ownership_lost=ownership_lost,
            probe_interval_s=0.01,
            recovery_window_s=0.15,
            retry_delay_s=0.01,
        ):
            # The first probe stalls, then reconnect remains unavailable. The
            # adapter's task survives every failure, including window exhaustion.
            execute = AsyncMock(side_effect=TimeoutError)
            monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncConnection.execute", execute)
            reconnect = MagicMock(side_effect=reconnect_error("offline"))
            monkeypatch.setattr(AsyncEngine, "connect", reconnect)
            await asyncio.wait_for(stopped.wait(), timeout=5)
            await watcher
            assert reconnect.call_count >= 3, "reconnect must back off and retry"
            bot._drain_and_close.assert_awaited_once()
            assert ownership_lost.is_set()
            current = asyncio.current_task()
            assert current is not None and current.cancelling() == 0
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_runtime_owner_leaves_the_turn_pool_capacity_available(
    db_nullpool_engine: AsyncEngine,
) -> None:
    from pathlib import Path

    from daimon.adapters.discord.runtime import build_runtime
    from daimon.core.config import Settings

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_nullpool_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
        }
    )
    async with build_runtime(settings) as runtime:
        engine = runtime.sessionmaker.kw["bind"]
        assert engine.pool.checkedout() == 0, "the owner connection must not consume a turn slot"
        async with runtime.sessionmaker() as session:
            assert await owner_is_alive(session, runtime.owner_key)
