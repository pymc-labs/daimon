"""Discord's `RoutinePoster`: post a routine's result to its destination.

Run by the bot's delivery poller (`daimon.core.routine_delivery`). The
destination is resolved here — a channel, or a thread, which on Discord is a
channel too — and must belong to the routine's own, live guild. A thread's
parent is fetched when Discord's cache does not hold it, because the parent's
category is what category protection is checked against; if it cannot be
resolved, nothing is posted there (fail closed). The tenant's access policy
then decides with the parent channel and category in hand.

When the destination cannot be used — protected, gone, or outside the guild —
the result goes to the routine's creator by direct message instead, if the
tenant's direct-message policy allows them, so it never silently goes
nowhere. A creator no longer allowed to invoke the agent gets nothing.
Mentions are disabled on every post: the text is the agent's, and a routine
must not ping anyone on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import (
    DM_FALLBACK_REASONS,
    DeliveryOutcome,
    check_delivery,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
)
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
) -> tuple[str | None, str | None, int | None] | None:
    """(parent_channel_id, category_id, guild_id) of a postable channel.

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
        )
    if isinstance(channel, discord.TextChannel):
        return (
            None,
            str(channel.category_id) if channel.category_id else None,
            channel.guild.id,
        )
    return None


def make_discord_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    dm_policy: DmPolicyLookup,
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
        target = delivery_target(row, platform="discord")
        if target is None or not target.channel_id.isdigit():
            return await _dm_fallback(row, "destination_unavailable", tenant.external_id)
        try:
            channel = await fetch_channel(int(target.channel_id))
        except discord.HTTPException:
            return await _dm_fallback(row, "destination_unavailable", tenant.external_id)
        placement = await _placement(channel, fetch_channel)
        if placement is None or str(placement[2]) != tenant.external_id:
            # Not a text channel or thread, its parent is unknown, or it is in
            # another guild.
            return await _dm_fallback(row, "destination_unavailable", tenant.external_id)
        parent_channel_id, category_id, _guild_id = placement
        refusal = await check_delivery(
            sessionmaker,
            row,
            platform="discord",
            target=target,
            parent_channel_id=parent_channel_id,
            category_id=category_id,
        )
        if refusal is not None:
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=refusal)
            if refusal in DM_FALLBACK_REASONS:
                return await _dm_fallback(row, refusal, tenant.external_id)
            return DeliveryOutcome(status="skipped", note=refusal)
        assert isinstance(channel, discord.Thread | discord.TextChannel)
        await channel.send(
            content=render_fallback_post(row)[:_DISCORD_MAX_CHARS],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return DeliveryOutcome(status="delivered")

    return _post
