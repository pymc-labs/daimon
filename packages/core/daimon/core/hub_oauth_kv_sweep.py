"""Prune expired rows from the hub login key-value table.

Every row the proxies write carries its own TTL (authorization codes: five
minutes, transactions: fifteen, upstream tokens: their upstream lifetime).
The store honours TTL on read but never deletes, so without this sweep the
table grows by one row per login attempt forever.

Shell-only: one session, one delete, one log line. ``now`` is injected and
the scheduler owns the boundary catch, as with the other sweepers.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from daimon.core.stores.hub_oauth_kv import delete_expired_hub_oauth_kv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


async def sweep_expired_hub_oauth_kv(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Delete up to ``limit`` expired rows. Returns the number deleted.

    A full batch means the sweep may not be keeping up, and one interval of
    deletions is no longer evidence the table is bounded, so it is reported at
    warning level as well.
    """
    async with session_factory() as session, session.begin():
        count = await delete_expired_hub_oauth_kv(session, cutoff=now, limit=limit)
    _log.info("hub_oauth_kv_sweep.expired", count=count)
    if count == limit:
        _log.warning("hub_oauth_kv_sweep.backlog", count=count, limit=limit)
    return count
