"""Delete ops events past `DAIMON_OPS_EVENTS__RETENTION_DAYS`, in bounded batches.

One batch per transaction and at most `max_batches` a run, under a statement timeout, as
for `monitoring_sweep`. Resolve and mute states left with no event to attach to go with
them. `now` is injected; the scheduler's call site owns the boundary catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from daimon.core.stores.ops_events import prune_issue_states, prune_ops_events
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

BATCH_SIZE: Final = 2000
#: 20,000 rows a run, hourly: well above what coalesced events a workspace writes in an hour.
MAX_BATCHES: Final = 10
STATEMENT_TIMEOUT_MS: Final = 30_000


async def sweep_ops_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    retention_days: int,
    batch: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES,
) -> int:
    """Delete events older than `retention_days`, at most `batch * max_batches`; the count."""
    cutoff = now - timedelta(days=retention_days)
    total = 0
    for _ in range(max_batches):
        async with session_factory() as session, session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            deleted = await prune_ops_events(session, cutoff=cutoff, batch=batch)
        total += deleted
        # A short batch: nothing older is left, or the rest is locked; the next run goes on.
        if deleted < batch:
            break
    async with session_factory() as session, session.begin():
        await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        states = await prune_issue_states(session, cutoff=cutoff)
    if total or states:
        _log.info("ops_events_sweep.pruned", count=total, states=states)
    return total
