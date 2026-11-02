"""One web process per deployment: a second one stops at start, a lost lock stops this one."""

from __future__ import annotations

import asyncio

import pytest
from daimon.adapters.web.single_process import (
    LOCK_KEY,
    WebProcessAlreadyRunning,
    hold_web_process_lock,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_a_second_web_process_is_refused_until_the_first_ends(
    db_committing_engine: AsyncEngine,
) -> None:
    async with hold_web_process_lock(db_committing_engine):
        with pytest.raises(WebProcessAlreadyRunning):
            async with hold_web_process_lock(db_committing_engine):
                raise AssertionError("a second holder must not get in")

    async with hold_web_process_lock(db_committing_engine):
        pass


async def test_a_dropped_lock_connection_stops_the_process_and_releases_cleanly(
    db_committing_engine: AsyncEngine,
) -> None:
    lost = asyncio.Event()
    async with hold_web_process_lock(db_committing_engine, ping_seconds=0.05, on_lost=lost.set):
        async with db_committing_engine.connect() as other:
            await other.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory'"
                    " AND granted AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": LOCK_KEY},
            )
        await asyncio.wait_for(lost.wait(), timeout=5)
    assert lost.is_set(), "the ping noticed the dead connection and called on_lost"
