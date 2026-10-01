"""Prune aged rows from the slack_event_dedup admission gate.

A `slack_event_dedup` row exists only to suppress redelivery of an event
Socket Mode has not yet been acked for. Slack's documented base retry
schedule is 3 retries over roughly 6 minutes; that extends to hourly retries
for 24 hours only if the "Delayed Events" app setting is enabled, which
daimon has not done (it is not in `docs/slack-app-manifest.yaml`). So
`_RETENTION` only has to outlive that window, and 7 days is 7x the
pessimistic 24-hour bound and roughly 1700x the actual ~6-minute one — it
also doubles as a one-week forensic trail for "did we ever receive that
event?". `_RETENTION` is a module constant, not a setting: `.env.example` is
generated from the Settings models, and a knob with a 1700x margin is not
worth that churn.

Deleting a row here never recovers a crashed turn — pruning is deliberately
out-of-band from any turn path, never per-turn.

Shell-only: opens one session, delegates the delete to the store's
select-keys-then-delete helper, commits, and logs the count. `now` is
injected, never read from a clock inside this module, and there is no
try/except here — the scheduler's call site owns the boundary catch, exactly
as it does for the existing sweepers (`wizard_sweep.py`,
`pending_file_sweeper.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from daimon.core.stores.slack_event_dedup import delete_event_dedup_older_than
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

# Not a setting — see the module docstring. A 1700x margin over the actual
# redelivery bound is not worth a config knob, and .env.example is generated.
_RETENTION: Final = timedelta(days=7)


async def sweep_expired_slack_event_dedup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Delete up to `limit` slack_event_dedup rows older than `_RETENTION`.

    Returns the number of rows deleted.
    """
    async with session_factory() as session, session.begin():
        count = await delete_event_dedup_older_than(session, cutoff=now - _RETENTION, limit=limit)
    _log.info("slack_event_dedup_sweep.expired", count=count)
    return count
