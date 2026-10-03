"""Store for `channel_skills`: the extra skills a channel's sessions run with.

One row per channel and skill, pinned to the version it was added at. No
rows means nothing extra. The caller decides who may write and which skill.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import ChannelSkill
from daimon.core.stores.domain import ChannelSkillRow
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def list_channel_skills(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str | None = None
) -> list[ChannelSkillRow]:
    """One channel's skills (every channel's with None), by channel then time added."""
    stmt = select(ChannelSkill).where(
        ChannelSkill.tenant_id == tenant_id, ChannelSkill.platform == platform
    )
    if channel_id is not None:
        stmt = stmt.where(ChannelSkill.channel_id == channel_id)
    stmt = stmt.order_by(ChannelSkill.channel_id, ChannelSkill.added_at, ChannelSkill.skill_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [ChannelSkillRow.model_validate(row) for row in rows]


async def add_channel_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    skill_id: str,
    version: str,
    name: str,
    owner_agent_name: str | None,
    actor_account_id: uuid.UUID | None,
) -> ChannelSkillRow:
    """Add one skill to a channel, or re-pin it to `version` when it is there already."""
    values = {
        "tenant_id": tenant_id,
        "platform": platform,
        "channel_id": channel_id,
        "skill_id": skill_id,
        "version": version,
        "name": name,
        "owner_agent_name": owner_agent_name,
        "added_by_account_id": actor_account_id,
    }
    stmt = (
        insert(ChannelSkill)
        .values(**values)
        .on_conflict_do_update(
            constraint="pk_channel_skills",
            set_={
                "version": version,
                "name": name,
                "owner_agent_name": owner_agent_name,
                "added_by_account_id": actor_account_id,
                "added_at": func.now(),
            },
        )
        .returning(ChannelSkill)
    )
    orm = (await session.execute(stmt, execution_options={"populate_existing": True})).scalar_one()
    await session.flush()
    return ChannelSkillRow.model_validate(orm)


async def remove_channel_skill(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str, skill_id: str
) -> bool:
    """Remove one skill from a channel. True when it was there."""
    result = await session.execute(
        delete(ChannelSkill).where(
            ChannelSkill.tenant_id == tenant_id,
            ChannelSkill.platform == platform,
            ChannelSkill.channel_id == channel_id,
            ChannelSkill.skill_id == skill_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


__all__ = ["add_channel_skill", "list_channel_skills", "remove_channel_skill"]
