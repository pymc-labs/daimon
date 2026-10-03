"""Contention with an independent PostgreSQL connection must release headroom."""

import asyncio
import os

import pytest
from daimon.core import session_mutation, session_preparation_gate
from daimon.core.db import build_engine
from daimon.core.turn.errors import SessionBusyError
from daimon.testing.db import build_test_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.parametrize("timeout", [False, True], ids=["proceeds", "busy"])
async def test_remote_fence_waiter_releases_permit_and_connection(
    db_nullpool_engine, db_schema, monkeypatch, timeout
):
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, pool_size=5, max_overflow=10
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    attempted = asyncio.Event()
    backoff = asyncio.Event()
    resume = asyncio.Event()
    original_lock = session_mutation.lock_session_mutation
    original_sleep = asyncio.sleep
    waiter = None

    async def observed_lock(*args, **kwargs):
        attempted.set()
        return await original_lock(*args, **kwargs)

    async def observed_sleep(delay, *args, **kwargs):
        if asyncio.current_task() is waiter:
            backoff.set()
            await resume.wait()
        return await original_sleep(delay, *args, **kwargs)

    async def send():
        async with session_mutation.session_mutation_fence(factory, "remote-holder", check=False):
            return "sent"

    monkeypatch.setattr(session_mutation, "lock_session_mutation", observed_lock)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    try:
        # A separate engine/connection has no knowledge of this process's gates.
        async with db_nullpool_engine.begin() as remote:
            await remote.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "session_mutation:remote-holder"},
            )
            waiter = asyncio.create_task(send())
            await asyncio.wait_for(attempted.wait(), 2)
            # Retry sleep starts only after transaction and gate teardown.
            await asyncio.wait_for(backoff.wait(), 2)
            assert not waiter.done()
            assert engine.pool.checkedout() == 0
            assert session_preparation_gate._pool_gates[engine.pool]._value == 5
            assert backoff.is_set(), "waiter must release resources before its retry sleep"
            # Unrelated fences still acquire while the remote owner holds its lock.
            async with asyncio.timeout(1):
                async with session_mutation.session_mutation_fence(
                    factory, "unrelated", check=False
                ):
                    pass
            if timeout:
                # Hold the external fence beyond the actual production wait bound.
                from daimon.core.turn.ceiling import TURN_CEILING_S

                loop = asyncio.get_running_loop()
                original_time = loop.time
                monkeypatch.setattr(loop, "time", lambda: original_time() + TURN_CEILING_S + 1)
                resume.set()
                with pytest.raises(SessionBusyError) as error:
                    await asyncio.wait_for(waiter, 1)
                assert error.value.pending_reasons == ("session_unavailable",)
                assert engine.pool.checkedout() == 0
        if not timeout:
            resume.set()
            assert await asyncio.wait_for(waiter, 1) == "sent"
    finally:
        resume.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("capacity", [2, 3])
def test_small_pool_rejected_at_engine_startup(capacity):
    with pytest.raises(ValueError, match=r"pool_size \+ max_overflow >= 4"):
        build_engine(
            "postgresql+asyncpg://u:p@localhost:1/test",
            pool_size=capacity,
            max_overflow=0,
        )
