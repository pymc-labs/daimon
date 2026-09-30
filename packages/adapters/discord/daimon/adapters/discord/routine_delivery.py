"""Discord's `RoutinePoster`: post a routine's result to its destination.

Run by the bot's delivery poller (`daimon.core.routine_delivery`). The
destination is resolved here — a channel, or a thread, which on Discord is a
channel too — and must belong to the routine's own, live guild. A thread's
parent is fetched when Discord's cache does not hold it, because the parent's
category is what category protection is checked against; if it cannot be
resolved, nothing is posted there (fail closed). The tenant's access policy
then decides with the parent channel and category in hand.

The creator is checked first, before anything is resolved or sent: if the
policy cannot be read, or the creator may no longer invoke the agent, nothing
is sent anywhere. Only a cleared result may then fall back: when the
destination cannot be used — protected, gone, or outside the guild — it goes
to the creator by direct message instead, if the tenant's direct-message
policy allows them, so it never silently goes nowhere.
Mentions are disabled on every post: the text is the agent's, and a routine
must not ping anyone on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from daimon.core.access_policy import TenantAccessPolicy, is_write_protected
from daimon.core.channel_isolation import load_channel_isolation
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    clear_creator,
    delivery_target,
    discord_creator_may_post,
    render_fallback_dm,
    render_fallback_post,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["make_discord_routine_poster"]

log = structlog.get_logger(__name__)

_DISCORD_MAX_CHARS = 2000

ChannelFetcher = Callable[[int], Awaitable[object]]
#: Open a DM with a member of a guild: `(guild_id, user_id)` → something with
#: `send`. Raises `discord.HTTPException` (or `LookupError`) when it cannot.
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
DmPolicyLookup = Callable[[RoutineRow], DirectMessagePolicy]

_UNRESOLVED = object()


async def _placement(
    channel: object, fetch_channel: ChannelFetcher
) -> tuple[str | None, str | None, int | None, object] | None:
    """(parent_channel_id, category_id, guild_id, permission_source) of a
    postable channel. `permission_source` is the channel whose permission
    overwrites apply: the channel itself, or a thread's parent.

    `None` when the channel is not a text channel or thread, or when a
    thread's parent cannot be resolved (so its category is unknown).
    """
    if isinstance(channel, discord.Thread):
        parent: object = channel.parent
        if parent is None and channel.parent_id:
            try:
                parent = await fetch_channel(channel.parent_id)
            except discord.HTTPException:
                parent = _UNRESOLVED
        if parent is _UNRESOLVED or parent is None:
            return None
        category = getattr(parent, "category_id", None)
        return (
            str(channel.parent_id) if channel.parent_id else None,
            str(category) if category else None,
            channel.guild.id,
            parent,
        )
    if isinstance(channel, discord.TextChannel):
        return (
            None,
            str(channel.category_id) if channel.category_id else None,
            channel.guild.id,
            channel,
        )
    return None


async def creator_may_post(channel: object, permission_source: object, user_id: int) -> bool:
    """Whether the routine's creator, as a member of the channel's guild, may
    post in `channel` — the rule `send_message` applies to a caller.

    Permissions come from `permission_source` (a thread's parent), so an
    uncached parent never breaks the check. A creator who has left the guild,
    or whose membership cannot be read, may not post.
    """
    if not isinstance(channel, discord.Thread | discord.TextChannel) or not isinstance(
        permission_source, discord.TextChannel | discord.ForumChannel
    ):
        return False
    guild = channel.guild
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return False
    perms = permission_source.permissions_for(member)
    is_thread = isinstance(channel, discord.Thread)
    is_private_thread = is_thread and channel.type is discord.ChannelType.private_thread
    is_thread_member = False
    if is_private_thread and not (member.guild_permissions.administrator or perms.manage_threads):
        assert isinstance(channel, discord.Thread)
        try:
            await channel.fetch_member(user_id)
            is_thread_member = True
        except discord.HTTPException:
            is_thread_member = False
    return discord_creator_may_post(
        administrator=member.guild_permissions.administrator,
        view_channel=perms.view_channel,
        send_messages=perms.send_messages,
        is_thread=is_thread,
        send_messages_in_threads=perms.send_messages_in_threads,
        is_private_thread=is_private_thread,
        manage_threads=perms.manage_threads,
        is_thread_member=is_thread_member,
    )


def make_discord_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    dm_policy: DmPolicyLookup,
    deployment_default: DeploymentDefault,
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    """A poster bound to the bot's channel lookup (cache first, then REST)."""

    async def _dm_fallback(row: RoutineRow, reason: str, guild_id: str) -> DeliveryOutcome:
        creator = row.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not guild_id.isdigit()
            or not dm_policy(row).allows(creator)
        ):
            return DeliveryOutcome(status="skipped", note=reason)
        try:
            dm = await open_dm(int(guild_id), int(creator))
            await dm.send(
                content=render_fallback_dm(row, reason)[:_DISCORD_MAX_CHARS],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, LookupError) as err:
            log.info("routine.delivery_dm_failed", routine_id=str(row.id), error=str(err))
            return DeliveryOutcome(status="skipped", note=reason)
        return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")

    async def _post(row: RoutineRow) -> DeliveryOutcome:
        async with sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        # Creator first, before resolving or sending anything: a creator who
        # may no longer invoke the agent, or an unreadable policy, gets
        # nothing — no destination post and no direct message.
        cleared = await clear_creator(sessionmaker, row, platform="discord")
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=cleared)
            return DeliveryOutcome(status="skipped", note=cleared)
        policy = cleared
        async with sessionmaker() as session:
            isolation = await load_channel_isolation(
                session, tenant_id=row.tenant_id, default=deployment_default, policy=policy
            )
        if isolation.routine_crosses(row):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="channel_isolated")
            return DeliveryOutcome(status="skipped", note="channel_isolated")
        keep_inside = isolation.keeps_routine_inside(row)

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:  # an isolated channel's result never leaves it, not even by DM
                return DeliveryOutcome(status="skipped", note=reason)
            return await _dm_fallback(row, reason, tenant.external_id)

        target = delivery_target(row, platform="discord")
        if target is None or not target.channel_id.isdigit():
            return await fallback("destination_unavailable")
        try:
            channel = await fetch_channel(int(target.channel_id))
        except discord.HTTPException:
            return await fallback("destination_unavailable")
        placement = await _placement(channel, fetch_channel)
        if placement is None or str(placement[2]) != tenant.external_id:
            # Not a text channel or thread, its parent is unknown, or it is in
            # another guild.
            return await fallback("destination_unavailable")
        parent_channel_id, category_id, _guild_id, permission_source = placement
        creator = row.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not await creator_may_post(channel, permission_source, int(creator))
        ):
            # A routine posts on its creator's behalf: never somewhere they
            # could not post themselves. Their own DM is still fine.
            log.info(
                "routine.delivery_refused", routine_id=str(row.id), reason="creator_cannot_post"
            )
            return await fallback("creator_cannot_post")
        if is_write_protected(
            policy,
            channel_id=target.channel_id,
            parent_channel_id=parent_channel_id,
            category_id=category_id,
        ):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="protected_channel")
            return await fallback("protected_channel")
        assert isinstance(channel, discord.Thread | discord.TextChannel)
        await channel.send(
            content=render_fallback_post(row)[:_DISCORD_MAX_CHARS],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return DeliveryOutcome(status="delivered")

    return _post
