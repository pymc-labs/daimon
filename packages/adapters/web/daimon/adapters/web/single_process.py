"""One web process per deployment.

Stream registries, approval cards and the start-up sweep that retires a dead process's
turns all live in one process's memory, so a second would interrupt the first's turns.
The first holds a Postgres advisory lock for its life; a second stops at start.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

LOCK_KEY = 0x6D6F6E5F776562
#: How often the lock's connection is pinged, and how long one ping may take.
PING_SECONDS = 15.0
PING_TIMEOUT_SECONDS = 10.0

log = structlog.get_logger(__name__)


class WebProcessAlreadyRunning(Exception):
    """Another web process holds the deployment's lock."""


def exit_process() -> None:
    """Stop at once: the lock is gone, so a second process may already be running."""
    os._exit(1)


async def _ping_until_lost(
    conn: AsyncConnection, *, every: float, on_lost: Callable[[], None]
) -> None:
    """Ping the lock's connection; the database drops the lock with a dead connection."""
    while True:
        await asyncio.sleep(every)
        try:
            async with asyncio.timeout(PING_TIMEOUT_SECONDS):
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError, TimeoutError) as err:
            log.error("web.process_lock.lost", exc_info=err)
            on_lost()
            return


@asynccontextmanager
async def hold_web_process_lock(
    engine: AsyncEngine,
    *,
    ping_seconds: float = PING_SECONDS,
    on_lost: Callable[[], None] = exit_process,
) -> AsyncIterator[None]:
    """Hold the deployment's web lock on a connection of its own, or raise
    `WebProcessAlreadyRunning`. The database drops it when the process dies, or when the
    connection does; a ping then calls `on_lost` (by default the process exits), since
    a second process could take the lock and interrupt this one's turns."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        held = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        ).scalar_one()
        if not held:
            raise WebProcessAlreadyRunning
        pinger = asyncio.create_task(_ping_until_lost(conn, every=ping_seconds, on_lost=on_lost))
        try:
            yield
        finally:
            pinger.cancel()
            with suppress(asyncio.CancelledError):
                await pinger
            # A pooled connection outlives this block, and its lock with it. A dead
            # connection took the lock with it, so there is nothing left to release.
            with suppress(SQLAlchemyError, OSError):
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
