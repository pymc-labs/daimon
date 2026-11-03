"""Remember the names Discord hands us on messages and interactions.

`/billing` names top spenders from the member cache or a fetch; when neither
answers (they left, the fetch timed out) it falls back to what was stored
here. Recording is best-effort and in the background
(`daimon.core.platform_names`): it never delays or fails the event.
"""

from __future__ import annotations

from collections.abc import Iterable

from daimon.core.channel_listing import store_listing
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import ChannelSighting, remember_channel_names, remember_user_name
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


async def list_guilds_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    guilds: Iterable[discord.Guild],
) -> int:
    """Store every guild's listed channels; one round of `run_channel_listing`."""
    stored = 0
    for guild in guilds:
        stored += await store_listing(
            sessionmaker,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild.id)),
            platform="discord",
            channels=listed_guild_channels(guild),
        )
    return stored
