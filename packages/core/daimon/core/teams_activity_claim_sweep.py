"""Bound Teams activity claim retention outside the delivery path."""

from __future__ import annotations

from datetime import datetime, timedelta

from daimon.core.stores.teams_activity_claims import delete_old
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RETENTION = timedelta(days=30)


async def sweep_old_teams_activity_claims(
    sm: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Prune at most 500 claims older than 30 days per scheduler tick."""
    async with sm() as session, session.begin():
        return await delete_old(session, cutoff=now - _RETENTION)
