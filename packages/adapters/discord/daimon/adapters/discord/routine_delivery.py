"""Discord's `RoutinePoster`: post a routine's result tail to its destination.

Run by the bot's delivery poller (`daimon.core.routine_delivery`). The
destination is resolved here — a channel, or a thread, which on Discord is a
channel too — and must belong to the routine's own guild. The tenant's access
policy is applied with the parent channel and category Discord reports, so a
protected channel, a thread under one, or a channel in a protected category
refuses the post. Mentions are disabled: the text is the agent's, and a
routine must not ping anyone on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    check_delivery,
    delivery_target,
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


def _placement(channel: object) -> tuple[str | None, str | None, int | None]:
    """(parent_channel_id, category_id, guild_id) of a postable channel."""
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        category = getattr(parent, "category_id", None) if parent is not None else None
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
    return None, None, None


def make_discord_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession], *, fetch_channel: ChannelFetcher
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    """A poster bound to the bot's channel lookup (cache first, then REST)."""

    async def _post(row: RoutineRow) -> DeliveryOutcome:
        target = delivery_target(row, platform="discord")
        if target is None or not target.channel_id.isdigit():
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
        try:
            channel = await fetch_channel(int(target.channel_id))
        except (discord.NotFound, discord.Forbidden):
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
        parent_channel_id, category_id, guild_id = _placement(channel)
        async with sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if (
            guild_id is None
            or tenant is None
            or str(guild_id) != tenant.external_id
            or not isinstance(channel, discord.Thread | discord.TextChannel)
        ):
            # Not a text channel or thread, or not in this routine's guild.
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
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
            return DeliveryOutcome(status="skipped", note=refusal)
        await channel.send(
            content=render_fallback_post(row)[:_DISCORD_MAX_CHARS],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return DeliveryOutcome(status="delivered")

    return _post
