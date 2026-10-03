"""Durable identity of Discord roles created for tenant agents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core._models import DiscordAgentRole
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AgentRole:
    ma_agent_id: str
    role_id: str
    agent_name: str


def _row(orm: DiscordAgentRole) -> AgentRole:
    return AgentRole(ma_agent_id=orm.ma_agent_id, role_id=orm.role_id, agent_name=orm.agent_name)


async def list_roles(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[AgentRole]:
    rows = await session.scalars(
        select(DiscordAgentRole).where(DiscordAgentRole.tenant_id == tenant_id)
    )
    return [_row(row) for row in rows]


async def roles_mentioned(
    session: AsyncSession, *, tenant_id: uuid.UUID, role_ids: list[str]
) -> list[AgentRole]:
    if not role_ids:
        return []
    rows = await session.scalars(
        select(DiscordAgentRole).where(
            DiscordAgentRole.tenant_id == tenant_id,
            DiscordAgentRole.role_id.in_(role_ids),
        )
    )
    return [_row(row) for row in rows]


async def save_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str,
    role_id: str,
    agent_name: str,
) -> None:
    await session.execute(
        insert(DiscordAgentRole)
        .values(
            tenant_id=tenant_id,
            ma_agent_id=ma_agent_id,
            role_id=role_id,
            agent_name=agent_name,
        )
        .on_conflict_do_update(
            constraint="pk_discord_agent_roles",
            set_={"role_id": role_id, "agent_name": agent_name},
        )
    )


async def delete_role(session: AsyncSession, *, tenant_id: uuid.UUID, ma_agent_id: str) -> None:
    await session.execute(
        delete(DiscordAgentRole).where(
            DiscordAgentRole.tenant_id == tenant_id,
            DiscordAgentRole.ma_agent_id == ma_agent_id,
        )
    )
