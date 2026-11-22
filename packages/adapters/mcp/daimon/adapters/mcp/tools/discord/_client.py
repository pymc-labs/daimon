"""Discord REST client and resolution helpers.

Per-call REST-only ``discord.Client`` with role-cache hydration for
``permissions_for`` correctness in REST-only mode.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import discord
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.outbound.mentions import ping_users
from discord.channel import _guild_channel_factory  # pyright: ignore[reportPrivateUsage]
from fastmcp.exceptions import ToolError

log = structlog.get_logger(__name__)

_application_ids: dict[int, int] = {}


@asynccontextmanager
async def rest_client(token: str) -> AsyncIterator[discord.Client]:
    """Per-call REST-only ``discord.Client``. Closes its HTTP session on exit."""
    # Never everyone, here or a role; only the people the rendered post tags. A send
    # that skipped the outbound gate pings nobody unless it says whom itself.
    users = ping_users.get()
    client = discord.Client(
        intents=discord.Intents.default(),
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=False if users is None else [discord.Object(int(u)) for u in users],
            replied_user=False,
        ),
    )
    me = await client.http.static_login(token)
    # Client.login would set this; REST-only mode must do it by hand so tools
    # can tell daimon's own threads apart (rename_thread's ownership check).
    client._connection.user = discord.ClientUser(state=client._connection, data=me)  # pyright: ignore[reportPrivateUsage]
    bot_id = client._connection.user.id  # pyright: ignore[reportPrivateUsage]
    client._connection.application_id = _application_ids.get(bot_id)  # pyright: ignore[reportPrivateUsage]
    try:
        yield client
    finally:
        await client.http.close()


async def ensure_application_id(client: discord.Client) -> int | None:
    """Load the application ID only for tools that handle agent webhooks."""
    if client.application_id is not None:
        return client.application_id
    if client.user is None:  # pyright: ignore[reportUnnecessaryComparison]
        return None
    try:
        application_id = int((await client.http.application_info())["id"])
    except discord.HTTPException:
        return None
    _application_ids[client.user.id] = application_id
    client._connection.application_id = application_id  # pyright: ignore[reportPrivateUsage]
    return application_id


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
        raise ToolError(
            "Discord tools are unavailable: this Daimon install isn't connected to "
            "Discord. Whoever runs this Daimon install must connect its Discord bot."
        )
    return discord_settings.bot_token.get_secret_value()


async def _resolve_member(  # pyright: ignore[reportUnusedFunction]  # used by _read/_search/_send
    c: discord.Client, guild_id: str, user_id: str
) -> tuple[discord.Guild, discord.Member]:
    """Fetch guild + member; the guild carries the roles REST-only ``permissions_for`` needs."""
    try:
        # `GET /guilds/{id}` returns the guild's roles and `Guild._from_data`
        # builds the role cache from them, so no separate roles request is needed.
        guild = await c.fetch_guild(int(guild_id))
    except discord.NotFound as e:
        raise ToolError("guild not found") from e
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
    except discord.InvalidData as e:
        # A channel type this discord.py does not know: it cannot be read or searched here.
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


async def _fetch_known_channels(  # pyright: ignore[reportUnusedFunction]  # used by _read/_search
    guild: discord.Guild,
) -> list[discord.abc.GuildChannel]:
    """The server's channels by REST, leaving out any type discord.py does not know.

    `Guild.fetch_channels` raises `InvalidData` for one unknown type, which would fail the
    whole call. On that error the raw list is read again and built with the library's own
    factory, skipping the unknown. A skipped channel is never listed and has no cached
    permission, so a read or search of it is resolved one by one and refused as not found
    (`_resolve_channel`). Only the count and the types are logged, never a name.

    A copy of `daimon.adapters.discord.audience_sync._fetch_known_channels`: the adapters
    may not import each other and `daimon-core` does not depend on discord.py. Keep the
    two in step.
    """
    try:
        return list(await guild.fetch_channels())
    except discord.InvalidData:
        pass
    raw: list[Any] = await guild._state.http.get_all_guild_channels(guild.id)  # pyright: ignore[reportPrivateUsage]
    channels: list[discord.abc.GuildChannel] = []
    skipped: list[int] = []
    for data in raw:
        factory, _ = _guild_channel_factory(data["type"])
        if factory is None:
            skipped.append(data["type"])
            continue
        channels.append(factory(guild=guild, state=guild._state, data=data))  # pyright: ignore[reportPrivateUsage]
    log.warning(
        "discord.channel_type_skipped",
        guild_id=str(guild.id),
        count=len(skipped),
        types=sorted(set(skipped)),
    )
    return channels
