"""Store for who can view each Discord room and active public thread, for data reads.

Written with the room facts, under their generation (`daimon.core.stores.audience_facts`),
so a change that makes the facts unknown makes the viewers unknown too. Unknown is None.
Viewers are reader subjects (`daimon.core.data_audience.user_subject`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from daimon.core._models import DiscordAudienceState, DiscordRoomViewers
from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

_BATCH = 1000


async def publish_viewers(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    generation: int,
    viewers: Mapping[str, frozenset[str]],
    now: datetime,
) -> None:
    """Replace the server's viewers with `viewers`, under the facts' `generation`."""
    await clear_viewers(session, tenant_id=tenant_id)
    rows = [
        {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "user_ids": sorted(users),
            "generation": generation,
            "computed_at": now,
        }
        for channel_id, users in viewers.items()
    ]
    for start in range(0, len(rows), _BATCH):
        await session.execute(insert(DiscordRoomViewers), rows[start : start + _BATCH])


async def clear_viewers(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    await session.execute(
        delete(DiscordRoomViewers).where(DiscordRoomViewers.tenant_id == tenant_id)
    )


async def read_viewers(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str, now: datetime
) -> frozenset[str] | None:
    """Who can view a room or active public thread, or None when unknown: no usable facts,
    or no row (a private or archived thread, a room over the cap, data sources off)."""
    found = await session.execute(
        select(DiscordRoomViewers.user_ids)
        .join(
            DiscordAudienceState,
            and_(
                DiscordAudienceState.tenant_id == DiscordRoomViewers.tenant_id,
                DiscordAudienceState.generation == DiscordRoomViewers.generation,
            ),
        )
        .where(
            DiscordRoomViewers.tenant_id == tenant_id,
            DiscordRoomViewers.channel_id == channel_id,
            DiscordAudienceState.complete.is_(True),
            DiscordAudienceState.fresh_until > now,
        )
    )
    users = found.scalar_one_or_none()
    return None if users is None else frozenset(users)
