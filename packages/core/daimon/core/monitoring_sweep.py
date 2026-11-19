"""Delete monitored-agent rows past `DAIMON_MONITORING__RETENTION_DAYS`, in bounded batches.

One batch per transaction, so no transaction holds more than a batch of row locks, and at
most `max_batches` a run, so a backlog spreads over runs. Each batch runs under a statement
timeout: the scheduler's sweeps share one loop. `now` is injected; the scheduler's call
site owns the boundary catch, as for the other sweeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from daimon.core.stores.monitoring import prune_before
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

BATCH_SIZE: Final = 2000
#: 20,000 rows a run, hourly: well above what a busy workspace's agents are asked a day.
MAX_BATCHES: Final = 10
STATEMENT_TIMEOUT_MS: Final = 30_000


async def sweep_monitored_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    retention_days: int,
    batch: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES,
) -> int:
    """Delete rows older than `retention_days`, at most `batch * max_batches`; the count."""
    cutoff = now - timedelta(days=retention_days)
    total = 0
    batches = 0
    for _ in range(max_batches):
        async with session_factory() as session, session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            deleted = await prune_before(session, cutoff=cutoff, batch=batch)
        batches += 1
        total += deleted
        # A short batch: nothing older is left, or the rest is locked; the next run goes on.
        if deleted < batch:
            break
    if total:
        _log.info("monitoring_sweep.pruned", count=total, batches=batches)
    return total
