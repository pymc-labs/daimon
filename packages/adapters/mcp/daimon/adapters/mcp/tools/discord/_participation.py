"""Discord-side check for channel-targeted tools: the caller must be able to see the channel.

Lives here, next to the read tools' visibility checks, because it is the same
permission model: the same REST client, the same member resolution, the same
parent-channel and private-thread rules as ``read_thread``.
"""

from __future__ import annotations

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
)


async def resolve_visible_channel(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    """The channel a budget applies to, once the caller is confirmed to see it.

    A thread resolves to its parent channel, which is what spend is attributed
    to; a private thread still needs the caller's membership (or Manage
    Threads) to be named here.
    """
    guild_id = _require_guild_id(auth)
    user_id = _require_discord_identity(auth)
    async with rest_client(_require_bot_token(runtime)) as client:
        _, member = await _resolve_member(client, guild_id, user_id)
        target = _require_guild_channel(await _resolve_channel(client, channel_id), guild_id)
        if isinstance(target, discord.Thread):
            await _check_thread_view(client, target, member, user_id)
            return str(target.parent_id)
        _check_view_permission(target, member)
        return str(target.id)
