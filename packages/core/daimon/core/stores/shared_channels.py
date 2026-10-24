"""Channels known to be shared with another organisation (`shared_channels`)."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core._models import SharedChannel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

SharedChannelSource = Literal["platform", "client"]


async def is_channel_shared(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> bool:
    """Whether the channel was ever recorded as shared. Once shared, always shared."""
    statement = select(SharedChannel.channel_id).where(
        SharedChannel.tenant_id == tenant_id,
        SharedChannel.platform == platform,
        SharedChannel.channel_id == channel_id,
    )
    return (await session.execute(statement)).first() is not None


async def mark_channel_shared(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    source: SharedChannelSource,
) -> None:
    """Record the channel as shared. Idempotent: the first record (and its source) is kept."""
    await session.execute(
        insert(SharedChannel)
        .values(tenant_id=tenant_id, platform=platform, channel_id=channel_id, source=source)
        .on_conflict_do_nothing(index_elements=["tenant_id", "platform", "channel_id"])
    )
