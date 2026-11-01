"""Each server's member list, which web access follows.

A full reload per server from the gateway's member chunks, then join, leave, role and
owner changes keep it current. Admin is the server owner, Administrator or Manage Server.
Needs the privileged server members intent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import structlog
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def tenant_of(guild_id: int) -> uuid.UUID:
    return derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))


def admin_flags(guild: discord.Guild, members: Iterable[discord.Member]) -> dict[str, bool]:
    """Each human member's id and whether they are a server admin."""
    return {
        str(m.id): is_member_guild_admin(m, guild_owner_id=guild.owner_id)
        for m in members
        if not m.bot
    }


class MemberSync:
    """Writes what the gateway says about a server's members."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock

    async def reload(self, guild: discord.Guild) -> None:
        """Replace the server's list with its full member list, for a live install."""
        if not guild.chunked:
            await guild.chunk(cache=True)
        tenant_id = tenant_of(guild.id)
        async with self._sessionmaker.begin() as session:
            tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.archived_at is not None:
                return
            members = admin_flags(guild, guild.members)
            await members_store.replace_members(
                session,
                tenant_id=tenant_id,
                members=members,
                workspace_name=guild.name,
                now=self._clock(),
            )
        log.info("discord.members.reloaded", guild_id=str(guild.id), members=len(members))

    async def reload_all(self, guilds: Iterable[discord.Guild]) -> None:
        """One server at a time, so chunking never floods the gateway; one failure
        leaves the rest to load."""
        for guild in guilds:
            try:
                await self.reload(guild)
            except (discord.ClientException, discord.HTTPException, TimeoutError) as err:
                log.warning(
                    "discord.members.reload_failed",
                    guild_id=str(guild.id),
                    err_type=type(err).__name__,
                )

    async def joined(self, member: discord.Member) -> None:
        """A member joined, or changed roles: listed, with their admin flag."""
        if member.bot:
            return
        async with self._sessionmaker.begin() as session:
            await members_store.upsert_member(
                session,
                tenant_id=tenant_of(member.guild.id),
                platform_user_id=str(member.id),
                is_admin=is_member_guild_admin(member, guild_owner_id=member.guild.owner_id),
                now=self._clock(),
            )

    async def left(self, guild_id: int, user_id: int) -> None:
        """A member left, was kicked or banned."""
        async with self._sessionmaker.begin() as session:
            await members_store.delete_member(
                session, tenant_id=tenant_of(guild_id), platform_user_id=str(user_id)
            )

    async def admins_changed(self, guild: discord.Guild, members: Iterable[discord.Member]) -> None:
        """Recompute the admin flag of `members` after a role or owner change."""
        flags = admin_flags(guild, members)
        if not flags:
            return
        async with self._sessionmaker.begin() as session:
            await members_store.update_admin_flags(
                session, tenant_id=tenant_of(guild.id), flags=flags, now=self._clock()
            )

    async def removed(self, guild_id: int) -> None:
        """The bot left the server: forget its list."""
        async with self._sessionmaker.begin() as session:
            await members_store.clear_members(session, tenant_id=tenant_of(guild_id))
