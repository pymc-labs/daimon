"""Lead reads from Discord's own surfaces: the core rule over gateway permission facts.

The private `/leads` queue and a lead approval card's Details both read here,
so they refuse exactly what `lead_intake_get` refuses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from cryptography.fernet import MultiFernet
from daimon.core.authz import AgentRef, Place
from daimon.core.lead_intake import IntakeConfig
from daimon.core.lead_intake_access import (
    LeadReader,
    LeadReadTarget,
    NativeChannel,
    authorize_lead_read,
    read_target,
)
from daimon.core.membership import MembershipResolver
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.lead_intake import IntakeStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord


class GatewayNativeLeadPermissions:
    """Native read facts for one member in one guild, fetched once per channel."""

    def __init__(self, guild: discord.Guild, member: discord.Member) -> None:
        self.guild, self.member = guild, member
        self._seen: dict[str, NativeChannel | None] = {}

    async def channel(self, channel_id: str) -> NativeChannel | None:
        if channel_id not in self._seen:
            self._seen[channel_id] = await self._fetch(channel_id)
        return self._seen[channel_id]

    async def _fetch(self, channel_id: str) -> NativeChannel | None:
        try:
            channel = await self.guild.fetch_channel(int(channel_id))
            if isinstance(channel, discord.Thread):
                parent = channel.parent
                if parent is None:
                    fetched = await self.guild.fetch_channel(channel.parent_id)
                    if not isinstance(fetched, discord.TextChannel | discord.ForumChannel):
                        return None
                    # Thread.permissions_for reads the parent from the guild cache.
                    self.guild._add_channel(fetched)  # pyright: ignore[reportPrivateUsage]
                    parent = fetched
                permissions = channel.permissions_for(self.member)
                private = channel.type is discord.ChannelType.private_thread
                member_of = not private or permissions.manage_threads
                if not member_of:
                    try:
                        await channel.fetch_member(self.member.id)
                        member_of = True
                    except discord.NotFound:
                        member_of = False
                return NativeChannel(
                    id=str(channel.id),
                    guild_id=str(channel.guild.id),
                    parent_id=str(channel.parent_id),
                    category_id=str(parent.category_id) if parent.category_id else None,
                    is_text=False,
                    is_thread=True,
                    private_thread=private,
                    everyone_can_view=parent.permissions_for(
                        parent.guild.default_role
                    ).view_channel,
                    can_view=permissions.view_channel,
                    can_read_history=permissions.read_message_history,
                    in_thread_or_manages=member_of,
                )
        except (discord.Forbidden, discord.NotFound, ValueError):
            return None
        permissions = channel.permissions_for(self.member)
        return NativeChannel(
            id=str(channel.id),
            guild_id=str(channel.guild.id),
            parent_id=None,
            category_id=str(channel.category_id) if channel.category_id else None,
            is_text=isinstance(channel, discord.TextChannel),
            is_thread=False,
            private_thread=False,
            everyone_can_view=channel.permissions_for(channel.guild.default_role).view_channel,
            can_view=permissions.view_channel,
            can_read_history=permissions.read_message_history,
            in_thread_or_manages=True,
        )


type CheckRead = Callable[[LeadReadTarget], Awaitable[None]]


@dataclass(frozen=True)
class DiscordLeadReads:
    """A tenant's intake store and the live read check for one person."""

    store: IntakeStore
    config: IntakeConfig
    check: CheckRead


async def discord_lead_reads(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: IntakeStore,
    resolver: MembershipResolver,
    tenant_id: UUID,
    guild: discord.Guild,
    user_id: str,
    origin: discord.abc.GuildChannel | discord.Thread | None,
) -> DiscordLeadReads:
    """The reader behind a person's own ephemeral reply: no agent carries what they read.

    Raises when lead intake is not configured for this guild or the person
    cannot be fetched; each read is then decided by `check`.
    """
    config, _ = await store.configuration()
    if str(guild.id) != config.guild_id:
        raise PermissionError("tenant_mismatch")
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    facts = await resolver.resolve(tenant_id, config.guild_id, user_id, require_fresh=True)
    member = await guild.fetch_member(int(user_id))
    origin_place = None
    origin_ids: frozenset[str] = frozenset()
    if isinstance(origin, discord.Thread):
        origin_place = Place.from_origin(
            parent_channel_id=str(origin.parent_id), thread_id=str(origin.id)
        )
        origin_ids = frozenset({str(origin.id), str(origin.parent_id)})
    elif origin is not None:
        origin_place = Place.from_origin(parent_channel_id=str(origin.id), thread_id=None)
        origin_ids = frozenset({str(origin.id)})
    reader = LeadReader(
        tenant_id=tenant_id,
        platform_user_id=user_id,
        facts=facts,
        policy=policy,
        config=config,
        agent_id=config.agent_id,
        agent=AgentRef.none(),
        origin=origin_place,
        origin_channel_ids=origin_ids,
    )
    native = GatewayNativeLeadPermissions(guild, member)

    async def check(target: LeadReadTarget) -> None:
        await authorize_lead_read(target, reader, native)

    return DiscordLeadReads(store=store, config=config, check=check)


async def require_lead_readable(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    resolver: MembershipResolver,
    tenant_id: UUID,
    channel: discord.abc.GuildChannel | discord.Thread,
    user_id: str,
    intake_id: str,
) -> tuple[str, ...]:
    """Authorize and render a live lead; approval cards retain no evidence."""
    store = IntakeStore(sessionmaker, fernet, tenant_id)
    reads = await discord_lead_reads(
        sessionmaker=sessionmaker,
        store=store,
        resolver=resolver,
        tenant_id=tenant_id,
        guild=channel.guild,
        user_id=user_id,
        origin=channel,
    )
    try:
        record = await store.get(UUID(intake_id))
    except ValueError as exc:
        raise LookupError("This lead is no longer available.") from exc
    if record.submission is None:
        raise LookupError("This lead is no longer available.")
    await reads.check(read_target(record))
    return (
        f"Intake: {record.id}",
        record.submission.model_dump_json(),
        record.progress.model_dump_json(),
    )
