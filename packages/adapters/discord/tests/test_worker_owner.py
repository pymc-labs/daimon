"""A lost owner connection fails stop; recovery waits out its live turns."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from secrets import randbits
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from asyncpg import CannotConnectNowError  # pyright: ignore[reportMissingTypeStubs]
from daimon.adapters.discord.runtime import hold_worker_owner
from daimon.core.stores.discord_message_admissions import claim_message
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    list_orphaned_turns,
    lock_orphaned_turn,
    mark_turn_active,
)
from daimon.core.stores.worker_ownership import owner_is_alive
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker


@pytest.fixture
def owner_key() -> int:
    """Advisory locks are database-wide, even with separate xdist schemas."""
    return randbits(30)


async def test_transient_owner_connection_loss_does_not_cancel_worker(
    db_nullpool_engine: AsyncEngine,
    owner_key: int,
) -> None:
    entered, stopped, ownership_lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
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
          AND pid IN (SELECT pid FROM pg_stat_activity
                      WHERE application_name = current_setting('application_name'))
    """)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with factory() as session:
            pid = await session.scalar(owner_pid, {"key": owner_key})
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        await asyncio.wait_for(_wait_for_second_update(lambda: attempts - 1), timeout=5)
        await asyncio.wait_for(_wait_for_lock(db_nullpool_engine, owner_key), timeout=5)
        await asyncio.sleep(0.1)
        assert not ownership_lost.is_set()
        assert not task.done(), "the worker must keep serving after reconnection"
        stopped.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_one_timed_out_heartbeat_recovers_without_stopping(
    db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, owner_key: int
) -> None:
    lost = asyncio.Event()
    blocked = asyncio.Event()
    release = asyncio.Event()
    original_execute = AsyncConnection.execute
    updates = 0
    owner_connections: list[AsyncConnection] = []
    owner_engine = MagicMock(spec=AsyncEngine)

    def connect() -> AsyncConnection:
        connection = db_nullpool_engine.connect()
        owner_connections.append(connection)
        return connection

    owner_engine.connect.side_effect = connect

    async def block_first_update(
        self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal updates
        if self in owner_connections and str(statement).lstrip().startswith(
            "UPDATE discord_worker_heartbeats"
        ):
            updates += 1
            if updates == 1:
                blocked.set()
                await release.wait()
        return await original_execute(self, statement, *args, **kwargs)

    async with hold_worker_owner(
        owner_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=0.01
    ):
        factory = async_sessionmaker(db_nullpool_engine)
        async with factory() as session:
            first_seen = await session.scalar(
                text("SELECT seen_at FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": owner_key},
            )
        # Limit the stalled update to the known owner connection.
        monkeypatch.setattr(AsyncConnection, "execute", block_first_update)
        await asyncio.wait_for(blocked.wait(), timeout=2)
        await asyncio.sleep(5.1)
        release.set()
        await asyncio.wait_for(_wait_for_second_update(lambda: updates), timeout=5)
        await asyncio.sleep(0.05)
        assert not lost.is_set()
        async with factory() as session:
            second_seen = await session.scalar(
                text("SELECT seen_at FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": owner_key},
            )
        assert first_seen is not None and second_seen is not None and second_seen > first_seen


async def _wait_for_second_update(count: Callable[[], int]) -> None:
    while count() < 2:
        await asyncio.sleep(0.01)


async def _wait_for_lock(engine: AsyncEngine, owner_key: int) -> None:
    factory = async_sessionmaker(engine)
    while True:
        async with factory() as session:
            held = await session.scalar(
                text("""SELECT EXISTS (SELECT 1 FROM pg_locks
                     WHERE locktype = 'advisory' AND granted AND classid = 0
                       AND objid = :key AND objsubid = 1)"""),
                {"key": owner_key},
            )
        if held:
            return
        await asyncio.sleep(0.02)


async def test_reconnect_after_grace_fails_stop(
    db_nullpool_engine: AsyncEngine, owner_key: int
) -> None:
    lost = asyncio.Event()
    owner_engine = MagicMock(spec=AsyncEngine)
    attempts = 0
    allow_reconnect = False

    def connect() -> AsyncConnection:
        nonlocal attempts
        attempts += 1
        if attempts > 1 and not allow_reconnect:
            raise ConnectionRefusedError("database unavailable")
        return db_nullpool_engine.connect()

    owner_engine.connect.side_effect = connect
    factory = async_sessionmaker(db_nullpool_engine)
    async with hold_worker_owner(
        owner_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=0.01
    ):
        async with factory() as session:
            pid = await session.scalar(
                text("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1
                     AND pid IN (SELECT pid FROM pg_stat_activity
                                 WHERE application_name = current_setting('application_name'))"""),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        await asyncio.wait_for(_wait_for_second_update(lambda: attempts), timeout=3)
        async with factory() as session:
            await session.execute(
                text("""UPDATE discord_worker_heartbeats
                     SET seen_at = clock_timestamp() - interval '181 seconds'
                     WHERE owner_key = :key"""),
                {"key": owner_key},
            )
            await session.commit()
        allow_reconnect = True
        await asyncio.wait_for(lost.wait(), timeout=5)
        assert attempts >= 3


async def test_startup_prunes_only_expired_heartbeats(
    db_nullpool_engine: AsyncEngine, owner_key: int
) -> None:
    factory = async_sessionmaker(db_nullpool_engine)
    locked = await db_nullpool_engine.connect()
    async with factory() as session:
        await session.execute(
            text("""INSERT INTO discord_worker_heartbeats (owner_key, seen_at)
                 VALUES (:expired, clock_timestamp() - interval '541 seconds'),
                        (:recent, clock_timestamp() - interval '179 seconds'),
                        (:locked, clock_timestamp() - interval '541 seconds')
                 ON CONFLICT (owner_key) DO UPDATE SET seen_at = EXCLUDED.seen_at"""),
            {"expired": owner_key + 1, "recent": owner_key + 2, "locked": owner_key + 3},
        )
        await session.commit()
    try:
        await locked.execute(text("SELECT pg_advisory_lock(:key)"), {"key": owner_key + 3})
        await locked.commit()
        async with (
            hold_worker_owner(
                db_nullpool_engine, owner_key=owner_key, ownership_lost=asyncio.Event()
            ),
            factory() as session,
        ):
            keys = set(
                (
                    await session.scalars(
                        text("""SELECT owner_key FROM discord_worker_heartbeats
                             WHERE owner_key IN (:expired, :recent, :owner, :locked)"""),
                        {
                            "expired": owner_key + 1,
                            "recent": owner_key + 2,
                            "owner": owner_key,
                            "locked": owner_key + 3,
                        },
                    )
                ).all()
            )
            assert keys == {owner_key, owner_key + 2, owner_key + 3}
    finally:
        await locked.close()


@pytest.mark.parametrize(
    "reconnect_error", [SQLAlchemyError, ConnectionRefusedError, CannotConnectNowError]
)
async def test_sustained_owner_loss_drains_without_cancellation(
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    reconnect_error: type[Exception],
    owner_key: int,
) -> None:
    from daimon.adapters.discord.__main__ import drain_on_owner_loss

    exit_process = MagicMock()
    monkeypatch.setattr("daimon.adapters.discord.__main__.os._exit", exit_process)
    monkeypatch.setattr("daimon.adapters.discord.runtime.OWNER_RETRY_BUDGET_S", 0.2)
    ownership_lost, stopped = asyncio.Event(), asyncio.Event()
    bot = MagicMock()
    bot.runtime.ownership_lost = ownership_lost
    bot._drain_and_close = AsyncMock(side_effect=lambda: stopped.set())
    watcher = asyncio.create_task(drain_on_owner_loss(bot))
    owner_connection = db_nullpool_engine.connect()
    owner_engine = MagicMock(spec=AsyncEngine)
    owner_engine.connect.side_effect = lambda: owner_connection
    original_execute = AsyncConnection.execute

    async def fail_owner_execute(
        self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if self is owner_connection:
            raise TimeoutError
        return await original_execute(self, statement, *args, **kwargs)

    try:
        async with hold_worker_owner(
            owner_engine,
            owner_key=owner_key,
            ownership_lost=ownership_lost,
            probe_interval_s=0.01,
        ):
            # Repeated failed probes exhaust the bounded reconnect period.
            monkeypatch.setattr(AsyncConnection, "execute", fail_owner_execute)
            reconnect = MagicMock(side_effect=reconnect_error("offline"))
            owner_engine.connect.side_effect = reconnect
            await asyncio.wait_for(stopped.wait(), timeout=5)
            await watcher
            assert reconnect.call_count >= 1
            bot._drain_and_close.assert_awaited_once()
            exit_process.assert_called_once_with(1)
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
        # The context-provider keep-warm task's first config read borrows a slot briefly.
        for _ in range(50):
            if engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.05)
        assert engine.pool.checkedout() == 0, "the owner connection must not consume a turn slot"
        async with runtime.sessionmaker() as session:
            assert await owner_is_alive(session, runtime.owner_key)


async def test_connection_loss_cannot_reclaim_live_pending_message(
    db_nullpool_engine: AsyncEngine,
    owner_key: int,
) -> None:
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    lost = asyncio.Event()
    async with factory() as session:
        tenant = await make_tenant(session)
        await session.commit()
    async with hold_worker_owner(
        db_nullpool_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=2
    ):
        async with factory() as session:
            assert await claim_message(
                session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=owner_key
            )
            await session.commit()
            pid = await session.scalar(
                text("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1
                     AND pid IN (SELECT pid FROM pg_stat_activity
                                 WHERE application_name = current_setting('application_name'))"""),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        async with factory() as session:
            assert not await claim_message(
                session,
                tenant_id=tenant.id,
                channel_id="1",
                message_id="2",
                owner_key=owner_key + 1,
            )
            await session.rollback()
        await asyncio.wait_for(_wait_for_lock(db_nullpool_engine, owner_key), timeout=5)
        assert not lost.is_set()


async def test_connection_loss_cannot_recover_live_card(
    db_nullpool_engine: AsyncEngine,
    owner_key: int,
) -> None:
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    lost = asyncio.Event()
    async with factory() as session:
        tenant = await make_tenant(session)
        row = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="owner-connection-loss",
            account_id=uuid4(),
            ma_session_id="owner-connection-loss",
        )
        await mark_turn_active(
            session,
            id=row.id,
            active_turn_message_id="live-card",
            now=datetime.now(UTC),
            owner_key=owner_key,
        )
        await session.commit()
        row = next(
            r for r in await list_orphaned_turns(session, platform="discord") if r.id == row.id
        )
    async with hold_worker_owner(
        db_nullpool_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=2
    ):
        async with factory() as session:
            pid = await session.scalar(
                text("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1
                     AND pid IN (SELECT pid FROM pg_stat_activity
                                 WHERE application_name = current_setting('application_name'))"""),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        async with factory() as session:
            assert await lock_orphaned_turn(session, row=row) is None
            await session.rollback()
        await asyncio.wait_for(_wait_for_lock(db_nullpool_engine, owner_key), timeout=5)
        assert not lost.is_set()
