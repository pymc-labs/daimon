"""A worker must stop when its dedicated PostgreSQL owner connection dies."""

import asyncio

import pytest
from daimon.adapters.discord.runtime import hold_worker_owner
from daimon.core.stores.worker_ownership import owner_is_alive
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


async def test_owner_connection_loss_cancels_the_worker(db_nullpool_engine: AsyncEngine) -> None:
    entered = asyncio.Event()
    owner_key = 718211

    async def worker() -> None:
        async with hold_worker_owner(
            db_nullpool_engine, owner_key=owner_key, probe_interval_s=0.01
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with factory() as session:
            assert await owner_is_alive(session, owner_key)
            pid = await session.scalar(
                text("""
                SELECT pid FROM pg_locks
                WHERE locktype = 'advisory' AND granted AND classid = 0
                  AND objid = :key AND objsubid = 1
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            """),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)
        async with factory() as session:
            assert not await owner_is_alive(session, owner_key)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
