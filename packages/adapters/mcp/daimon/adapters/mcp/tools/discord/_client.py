"""Discord REST client and resolution helpers.

Per-call REST-only ``discord.Client`` with role-cache hydration for
``permissions_for`` correctness in REST-only mode.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from fastmcp.exceptions import ToolError


@asynccontextmanager
async def rest_client(token: str) -> AsyncIterator[discord.Client]:
    """Per-call REST-only ``discord.Client``. Closes its HTTP session on exit."""
    client = discord.Client(intents=discord.Intents.default())
    await client.http.static_login(token)
    try:
        yield client
    finally:
        await client.http.close()


def _require_discord_identity(auth: AuthIdentity) -> str:  # pyright: ignore[reportUnusedFunction]  # re-exported
    if auth.platform_user_id is None:
        raise ToolError("discord tools require a discord-bound identity")
    return auth.platform_user_id


def _require_guild_id(auth: AuthIdentity) -> str:  # pyright: ignore[reportUnusedFunction]  # re-exported
    if auth.external_id is None:
        raise ToolError("discord tools require a guild context")
    return auth.external_id


def _require_bot_token(runtime: McpRuntime) -> str:  # pyright: ignore[reportUnusedFunction]  # used by _read/_search/_send
    """Read the bot token from settings (presence validated at boot in Plan 24-04)."""
    discord_settings = runtime.settings.discord
    if discord_settings is None:
        raise ToolError("discord tools require DAIMON_DISCORD__BOT_TOKEN")
    return discord_settings.bot_token.get_secret_value()


async def _resolve_member(  # pyright: ignore[reportUnusedFunction]  # used by _read/_search/_send
    c: discord.Client, guild_id: str, user_id: str
) -> tuple[discord.Guild, discord.Member]:
    """Fetch guild + member; hydrate the role cache for REST-only ``permissions_for``."""
    try:
        guild = await c.fetch_guild(int(guild_id))
    except discord.NotFound as e:
        raise ToolError("guild not found") from e
    # REST-only mode does not hydrate guild.roles. Without this hydration
    # permissions_for(member) sees only @everyone and role-specific overrides
    # are ignored. No public API for this in REST-only mode.
    for role in await guild.fetch_roles():
        guild._add_role(role)  # pyright: ignore[reportPrivateUsage]
    # Register this hydrated guild in the connection-state cache so a subsequent
    # ``client.fetch_channel`` reuses it instead of minting a new (empty) guild
    # via ``_get_or_create_unavailable_guild`` — which would lose the role cache
    # and break ``channel.permissions_for(member)``.
    c._connection._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
    try:
        member = await guild.fetch_member(int(user_id))
    except discord.NotFound as e:
        raise ToolError("caller is not a member of this guild") from e
    return guild, member


async def _resolve_channel(  # pyright: ignore[reportUnusedFunction]  # used by _read/_search/_send
    c: discord.Client, channel_id: str
) -> discord.abc.GuildChannel | discord.Thread | discord.DMChannel:
    try:
        channel = await c.fetch_channel(int(channel_id))
    except discord.NotFound as e:
        raise ToolError("channel not found") from e
    if isinstance(channel, (discord.abc.GuildChannel, discord.Thread, discord.DMChannel)):
        return channel
    # GroupChannel and other partial types are not supported.
    raise ToolError("dm channels are not supported")


def _require_guild_channel(  # pyright: ignore[reportUnusedFunction]  # used by _read/_send
    channel: discord.abc.GuildChannel | discord.Thread | discord.DMChannel,
    expected_guild_id: str,
) -> discord.abc.GuildChannel | discord.Thread:
    """Reject DMs and cross-guild access. Returns the narrowed guild channel."""
    if isinstance(channel, discord.DMChannel):
        raise ToolError("dm channels are not supported")
    if str(channel.guild.id) != expected_guild_id:
        raise ToolError("channel not in this guild")
    return channel
