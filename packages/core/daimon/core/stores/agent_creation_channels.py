"""The channel each agent was created for by a channel admin of it: one row per agent.

No row means the agent was made by a server admin, outside a channel, or
before this was recorded. Per `guideline:architecture`, exceptions propagate.
"""

from __future__ import annotations

import uuid

from daimon.core._models import AgentCreationChannel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_creation_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID, ma_agent_id: str, platform: str, channel_id: str
) -> None:
    """Record it once: an agent id is created once, so a second write changes nothing."""
    await session.execute(
        pg_insert(AgentCreationChannel)
        .values(
            tenant_id=tenant_id, ma_agent_id=ma_agent_id, platform=platform, channel_id=channel_id
        )
        .on_conflict_do_nothing(constraint="pk_agent_creation_channels")
    )


async def get_creation_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID, ma_agent_id: str, platform: str
) -> str | None:
    """The channel the agent was created for on `platform`, or None."""
    return await session.scalar(
        select(AgentCreationChannel.channel_id).where(
            AgentCreationChannel.tenant_id == tenant_id,
            AgentCreationChannel.ma_agent_id == ma_agent_id,
            AgentCreationChannel.platform == platform,
        )
    )
