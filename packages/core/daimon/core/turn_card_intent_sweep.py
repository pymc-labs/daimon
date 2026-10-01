"""Prune retired initial-card intents after their recovery history expires.

Retired intents are no longer needed for recovery. Keep them for seven days
as a short forensic trail, matching the finite-retention event dedup record,
then remove them in bounded batches. Active intents are never eligible.
The scheduler calls this out-of-band; cleanup never runs on a turn path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from daimon.core.stores.turn_card_intents import delete_retired_turn_card_intents
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_RETENTION: Final = timedelta(days=7)
_BATCH_SIZE: Final = 500


async def sweep_retired_turn_card_intents(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> int:
    """Delete at most 500 retired intents older than the seven-day window."""
    async with session_factory() as session, session.begin():
        count = await delete_retired_turn_card_intents(
            session,
            cutoff=now - _RETENTION,
            batch_size=_BATCH_SIZE,
        )
    _log.info("turn_card_intent_sweep.retired", count=count)
    return count
