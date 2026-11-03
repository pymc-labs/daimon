"""Remember the names Discord hands us on messages and interactions.

`/billing` names top spenders from the member cache or a fetch; when neither
answers (they left, the fetch timed out) it falls back to what was stored
here. Recording is best-effort and in the background
(`daimon.core.platform_names`): it never delays or fails the event.
"""

from __future__ import annotations

from collections.abc import Iterable

from daimon.core.channel_listing import (
    check_unlisted_channels,
    discord_channel_answer,
    store_listing,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import (
    ChannelLookup,
    ChannelSighting,
    ChannelStatus,
    remember_channel_names,
    remember_user_name,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord


def remember_guild_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    guild_id: int | None,
    user: discord.User | discord.Member,
) -> None:
    """Store a guild user's server display name and username; bots and DMs are skipped."""
    if guild_id is None or user.bot:
        return
    remember_user_name(
        sessionmaker,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
        platform="discord",
        user_id=str(user.id),
        display_name=user.display_name,
        handle=user.name,
    )


def remember_guild_channels(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    guild_id: int | None,
    channels: Iterable[object],
) -> None:
    """Store the names of guild channels, as the gateway cache already holds them.

    A thread is stored under its parent, the channel a person configures; categories,
    voice channels and direct messages are skipped. Never calls Discord.
    """
    if guild_id is None:
        return
    names: dict[str, str | ChannelSighting] = {}
    for channel in channels:
        target = channel.parent if isinstance(channel, discord.Thread) else channel
        if isinstance(target, (discord.TextChannel, discord.ForumChannel)):
            category = target.category
            names[str(target.id)] = ChannelSighting(
                target.name,
                kind="forum" if isinstance(target, discord.ForumChannel) else "text",
                parent_id=str(category.id) if category is not None else None,
                parent_name=category.name if category is not None else None,
            )
    if names:
        remember_channel_names(
            sessionmaker,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
            platform="discord",
            names=names,
        )


def listed_guild_channels(guild: discord.Guild) -> dict[str, ChannelSighting]:
    """The guild's text and forum channels and live threads the bot can see, from the cache."""
    me = guild.me
    found: dict[str, ChannelSighting] = {}
    for channel in (*guild.channels, *guild.threads):
        if not channel.permissions_for(me).view_channel:
            continue
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            found[str(channel.id)] = ChannelSighting(
                channel.name,
                kind="thread",
                parent_id=str(parent.id) if parent is not None else None,
                parent_name=parent.name if parent is not None else None,
                archived=channel.archived,
            )
        elif isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            category = channel.category
            found[str(channel.id)] = ChannelSighting(
                channel.name,
                kind="forum" if isinstance(channel, discord.ForumChannel) else "text",
                parent_id=str(category.id) if category is not None else None,
                parent_name=category.name if category is not None else None,
                archived=False,
            )
    return found


def bot_channel_lookup(client: discord.Client) -> ChannelLookup:
    """Ask Discord, through the bot's own client, what one channel id is.

    The client waits out Discord's rate limits itself. Ids that are not numbers (web chat,
    a routine's destination) are no Discord channel.
    """

    async def lookup(guild_id: str, channel_id: str) -> str | ChannelStatus | None:
        if not channel_id.isdigit():
            return None
        try:
            fields = await client.http.get_channel(int(channel_id))
        except discord.HTTPException as exc:
            return discord_channel_answer(exc.status, {"code": exc.code}, guild_id)
        return discord_channel_answer(200, fields, guild_id)

    return lookup


#: Seconds between the status checks of one round: a handful a second at most.
_CHECK_GAP_S = 0.3


async def list_guilds_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    guilds: Iterable[discord.Guild],
    *,
    lookup: ChannelLookup | None = None,
) -> int:
    """Store every guild's listed channels; one round of `run_channel_listing`.

    With a `lookup`, channels that have settings but are not in the list are asked about
    too (a few a round): the ones Discord says are deleted or direct messages are marked.
    """
    stored = 0
    for guild in guilds:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
        listed = listed_guild_channels(guild)
        stored += await store_listing(
            sessionmaker, tenant_id=tenant_id, platform="discord", channels=listed
        )
        if lookup is not None:
            await check_unlisted_channels(
                sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                external_id=str(guild.id),
                listed=listed.keys(),
                lookup=lookup,
                gap_s=_CHECK_GAP_S,
            )
    return stored
