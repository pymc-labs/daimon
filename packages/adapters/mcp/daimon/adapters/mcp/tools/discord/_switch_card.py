"""Post Daimon's own provider switch card to a Discord thread.

For a turn whose provider shows no confirmation card (OpenAI, Gemini): the
card is posted from the MCP process and pressed in the Discord bot process
(`daimon.adapters.discord.posted_controls.switch_card`), which re-verifies the
switch before applying it. Same require/resolve/permission-check order as the
credential card (`_credential_button`), so it reaches only a place the
requester can see and Daimon may write.
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
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._posted_card import build_card_view
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.authz import Place
from daimon.core.posted_controls import PostedCard
from fastmcp.exceptions import ToolError


async def post_switch_card(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    card: PostedCard,
    origin: Place,
) -> str:
    """Post `card` into `channel_id` (the turn's thread); the sent message's id."""
    requester_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    bot_token = _require_bot_token(runtime)
    view = build_card_view(card)
    async with rest_client(bot_token) as c:
        _, member = await _resolve_member(c, guild_id, requester_id)
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            await _ensure_thread_parent_cached(channel)
            await _check_thread_view(c, channel, member, requester_id)
        _check_send_permission(channel, member)
        await _require_discord_channel_writable(runtime, auth, channel, origin=origin)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support sending messages")
        sent = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False, replied_user=False
            ),
        )
        return str(sent.id)
