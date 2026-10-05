"""Best-effort Discord role reconciliation for mentionable tenant agents."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import agent_names
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.permissions import agent_permissions
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.discord_agent_roles import delete_role, list_roles, save_role
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()
_missing_manage_roles: set[int] = set()
_sync_locks: dict[int, asyncio.Lock] = {}


async def sync_agent_roles(
    *,
    guild: discord.Guild,
    tenant_id: uuid.UUID,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    agents: Sequence[BetaManagedAgentsAgent] | None = None,
) -> None:
    """Backfill, rename, and remove managed roles; never interrupt a chat turn."""
    lock = _sync_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        await _sync_agent_roles_unlocked(
            guild=guild,
            tenant_id=tenant_id,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            agents=agents,
        )


async def _sync_agent_roles_unlocked(
    *,
    guild: discord.Guild,
    tenant_id: uuid.UUID,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    agents: Sequence[BetaManagedAgentsAgent] | None,
) -> None:
    member: discord.Member | None = getattr(guild, "me", None)
    if member is None or not member.guild_permissions.manage_roles:
        if guild.id not in _missing_manage_roles:
            log.warning("agent_roles.manage_roles_missing", guild_id=str(guild.id))
            _missing_manage_roles.add(guild.id)
        return
    _missing_manage_roles.discard(guild.id)
    try:
        if agents is None:
            agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
            stored = {
                row.ma_agent_id: row for row in await list_roles(session, tenant_id=tenant_id)
            }
        eligible: dict[str, BetaManagedAgentsAgent] = {}
        undecided: set[str] = set()
        for agent in agents:
            try:
                permissions = agent_permissions(policy, agent_names(agent.name, agent.metadata))
                runnable = not permissions.runs_in or bool(
                    set(permissions.runs_in[0]).intersection(*permissions.runs_in[1:])
                )
                if permissions.home is None and runnable:
                    eligible[agent.id] = agent
            except Exception:
                undecided.add(agent.id)
                log.exception(
                    "agent_roles.eligibility_failed", guild_id=str(guild.id), agent_id=agent.id
                )
        for agent_id, row in stored.items():
            if agent_id in eligible or agent_id in undecided:
                continue
            try:
                role = guild.get_role(int(row.role_id))
                if role is not None:
                    await role.delete(reason="Agent archived or has a home or runs nowhere")
                async with sessionmaker.begin() as session:
                    await delete_role(session, tenant_id=tenant_id, ma_agent_id=agent_id)
            except Exception:
                log.exception(
                    "agent_roles.delete_failed", guild_id=str(guild.id), agent_id=agent_id
                )
        for agent_id, agent in eligible.items():
            created_role_id: str | None = None
            try:
                row = stored.get(agent_id)
                role = guild.get_role(int(row.role_id)) if row is not None else None
                if role is None:
                    role = await guild.create_role(
                        name=agent.name,
                        mentionable=True,
                        reason="Mentionable Daimon agent",
                    )
                    created_role_id = str(role.id)
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
            except Exception:
                log.exception(
                    "agent_roles.agent_sync_failed", guild_id=str(guild.id), agent_id=agent_id
                )
                if created_role_id is not None:
                    log.warning(
                        "agent_roles.orphaned_role",
                        guild_id=str(guild.id),
                        agent_id=agent_id,
                        role_id=created_role_id,
                    )
    except (discord.HTTPException, discord.ClientException) as exc:
        log.warning("agent_roles.sync_failed", guild_id=str(guild.id), error=str(exc))
    except Exception:
        log.exception("agent_roles.sync_failed", guild_id=str(guild.id))
