"""Best-effort Discord role reconciliation for mentionable tenant agents."""

from __future__ import annotations

import uuid

import structlog
from anthropic import AsyncAnthropic
from daimon.core.access_policy import isolation_owner
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.discord_agent_roles import delete_role, list_roles, save_role
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()


async def sync_agent_roles(
    *,
    guild: discord.Guild,
    tenant_id: uuid.UUID,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Backfill, rename, and remove managed roles; never interrupt a chat turn."""
    member: discord.Member | None = getattr(guild, "me", None)
    if member is None or not member.guild_permissions.manage_roles:
        log.warning("agent_roles.manage_roles_missing", guild_id=str(guild.id))
        return
    try:
        agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
            stored = {
                row.ma_agent_id: row for row in await list_roles(session, tenant_id=tenant_id)
            }
        eligible = {
            agent.id: agent
            for agent in agents
            if isolation_owner(policy, agent_pin_names(agent.name, agent.metadata)) is None
        }
        for agent_id, row in stored.items():
            if agent_id in eligible:
                continue
            role = guild.get_role(int(row.role_id))
            if role is not None:
                await role.delete(reason="Agent archived or confined to a confidential channel")
            async with sessionmaker.begin() as session:
                await delete_role(session, tenant_id=tenant_id, ma_agent_id=agent_id)
        for agent_id, agent in eligible.items():
            row = stored.get(agent_id)
            role = guild.get_role(int(row.role_id)) if row is not None else None
            if role is None:
                role = await guild.create_role(
                    name=agent.name,
                    mentionable=True,
                    reason="Mentionable Daimon agent",
                )
            elif role.name != agent.name or not role.mentionable:
                await role.edit(name=agent.name, mentionable=True, reason="Agent name changed")
            async with sessionmaker.begin() as session:
                await save_role(
                    session,
                    tenant_id=tenant_id,
                    ma_agent_id=agent_id,
                    role_id=str(role.id),
                    agent_name=agent.name,
                )
    except (discord.HTTPException, discord.ClientException) as exc:
        log.warning("agent_roles.sync_failed", guild_id=str(guild.id), error=str(exc))
    except Exception:
        log.exception("agent_roles.sync_failed", guild_id=str(guild.id))
