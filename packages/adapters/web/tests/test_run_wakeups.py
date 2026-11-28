"""Open run pages share one listener connection and still wake on their run's notice."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AsyncExitStack
from typing import cast

from daimon.adapters.web.run_wakeups import RunWakeups
from daimon.core.workflows.runs import run_channel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool


async def test_watch_when_many_pages_are_open_then_one_connection_listens_and_each_run_wakes(
    db_engine: AsyncEngine,
) -> None:
    tenant, mine, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    wakeups = RunWakeups(db_engine, max_streams=21)
    before = cast(QueuePool, db_engine.sync_engine.pool).checkedout()
    async with AsyncExitStack() as stack:
        mine_woke = [
            await stack.enter_async_context(wakeups.watch(tenant, mine)) for _ in range(20)
        ]
        other_woke = await stack.enter_async_context(wakeups.watch(tenant, other))
        held = cast(QueuePool, db_engine.sync_engine.pool).checkedout() - before
        assert held == 1, f"21 open pages hold {held} pooled connections, not one each"
        assert wakeups.full, "the stream cap is reached"
        async with db_engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_notify(:c, :p)"), {"c": run_channel(tenant), "p": str(mine)}
            )
        await asyncio.wait_for(asyncio.gather(*(w.wait() for w in mine_woke)), timeout=5)
        assert not other_woke.is_set(), "another run's page stays asleep"
    assert db_engine.pool.checkedout() == before, "the last page closing frees the connection"  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
