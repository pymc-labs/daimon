"""Store for `channel_skills`: the extra skills a channel's sessions run with.

One row per channel and skill, pinned to the version it was added at. No
rows means nothing extra. The caller decides who may write and which skill.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import ChannelSkill
from daimon.core.stores.channel_changes import record_change
from daimon.core.stores.domain import ChangeSurface, ChannelSkillRow
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
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    skill_id: str,
    actor_account_id: uuid.UUID | None,
    surface: ChangeSurface,
) -> bool:
    """Remove one skill from a channel and record it. True when it was there."""
    current = await list_channel_skills(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    result = await session.execute(
        delete(ChannelSkill).where(
            ChannelSkill.tenant_id == tenant_id,
            ChannelSkill.platform == platform,
            ChannelSkill.channel_id == channel_id,
            ChannelSkill.skill_id == skill_id,
        )
    )
    await session.flush()
    removed = cast(CursorResult[Any], result).rowcount > 0
    if removed:
        await record_change(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            setting="skills",
            before=[r.name for r in current],
            after=[r.name for r in current if r.skill_id != skill_id],
            actor_account_id=actor_account_id,
            surface=surface,
        )
    return removed


async def list_skill_channel_rules(
    session: AsyncSession, *, skill_id: str
) -> list[ChannelSkillRow]:
    """Every channel rule, in any tenant or platform, that adds `skill_id`.

    Skills are workspace-wide on Managed Agents, so a rule anywhere still
    resolves this skill at session creation and fails once it is deleted.
    """
    stmt = (
        select(ChannelSkill)
        .where(ChannelSkill.skill_id == skill_id)
        .order_by(ChannelSkill.platform, ChannelSkill.channel_id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [ChannelSkillRow.model_validate(row) for row in rows]


async def list_channel_skill_ids(session: AsyncSession) -> set[str]:
    """The skill ids any channel rule adds, across every tenant and platform."""
    rows = (await session.execute(select(ChannelSkill.skill_id).distinct())).scalars().all()
    return set(rows)


async def remove_skill_from_every_channel(session: AsyncSession, *, skill_id: str) -> int:
    """Remove `skill_id` from every channel rule. Returns how many rules held it."""
    result = await session.execute(delete(ChannelSkill).where(ChannelSkill.skill_id == skill_id))
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


__all__ = [
    "add_channel_skill",
    "list_channel_skill_ids",
    "list_channel_skills",
    "list_skill_channel_rules",
    "remove_channel_skill",
    "remove_skill_from_every_channel",
]


async def list_tenant_channel_skills(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[ChannelSkillRow]:
    """Every channel skill rule of the tenant, on any platform."""
    rows = await session.scalars(
        select(ChannelSkill)
        .where(ChannelSkill.tenant_id == tenant_id)
        .order_by(ChannelSkill.platform, ChannelSkill.channel_id, ChannelSkill.skill_id)
    )
    return [ChannelSkillRow.model_validate(row) for row in rows]
