"""Delete closed rows from github_issued_tokens in bounded batches.

Rows pile up at about 3,000 a day, nearly all `revoked`. No lookup reads them:
every list and select filters to `stored` or `delivered`. Only `mark_revoked` and
`record_revoke_attempt` touch a revoked row, by id, and a caller delayed past the
7-day retention gets a missing-row error for a token that expired days before. The store's
`prune_closed_tokens` defines what is closed (revoked 7 days past expiry, or a
never-stored `pending` row 1 hour past expiry) and never touches `stored` or
`delivered` rows or the `security_audit_events` log. The cutoffs and the
reasoning are documented there.

This shell commits one batch at a time, so no transaction holds more than a
batch of row locks, and stops after `max_batches`, so one run is bounded even
behind a large backlog; the next run continues. Each batch also runs under a
statement timeout: the scheduler runs its sweeps one after another on one loop,
and a DELETE stuck behind a schema change must not stall them all. `now` is
injected, never read here, and the scheduler's call site owns the boundary
catch, as for the other sweeps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

import structlog
from daimon.core.stores.github_issued_tokens import prune_closed_tokens
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

BATCH_SIZE: Final = 2000
#: 10 batches of 2,000 is 20,000 rows a run; a run every 10 minutes keeps up
#: with about 3,000 new closed rows a day.
MAX_BATCHES: Final = 10
#: A 2,000-row batch takes milliseconds; this only ends a batch that is stuck.
STATEMENT_TIMEOUT_MS: Final = 30_000


async def sweep_closed_github_issued_tokens(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    batch: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES,
) -> int:
    """Delete closed token rows, at most `batch * max_batches`; return the count."""
    total = 0
    batches = 0
    for _ in range(max_batches):
        async with session_factory() as session, session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            deleted = await prune_closed_tokens(session, now=now, batch=batch)
        batches += 1
        total += deleted
        # A short batch means nothing eligible is left, or the rest is locked
        # by a writer; the next run picks those up.
        if deleted < batch:
            break
    if total:
        _log.info("github_issued_token_sweep.pruned", count=total, batches=batches)
    return total
