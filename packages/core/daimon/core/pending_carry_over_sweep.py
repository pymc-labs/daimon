"""Drop carry-over framing whose session was never used again.

A row lives until its session's first message is sent. A session abandoned
before that (the thread moved on, or recovery replaced it) leaves its row; after
a week nobody will send it. The scheduler calls this out-of-band, in bounded
batches, matching `turn_card_intent_sweep`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from daimon.core.stores.pending_carry_overs import delete_stale_pending_carry_overs
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_RETENTION: Final = timedelta(days=7)
_BATCH_SIZE: Final = 500


async def sweep_stale_pending_carry_overs(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Delete at most 500 rows older than the seven-day window."""
    async with session_factory() as session, session.begin():
        count = await delete_stale_pending_carry_overs(
            session, cutoff=now - _RETENTION, batch_size=_BATCH_SIZE
        )
    _log.info("pending_carry_over_sweep.deleted", count=count)
    return count
