"""Discord-side check for the thread-participation tools: caller ids must be real and visible.

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
from daimon.core.thread_participation import ParticipationScope
from fastmcp.exceptions import ToolError


async def verify_participation_scope(
    runtime: McpRuntime,
    auth: AuthIdentity,
    scope: ParticipationScope,
    scope_id: str | None,
) -> str | None:
    """The ids are caller-supplied: confirm they name something in this guild the caller can see.

    Rows are tenant-scoped, so a foreign id could never leak across tenants;
    this closes the within-tenant gap where a member names a private thread
    they are not in and makes the bot speak (and spend) there. Returns the
    thread's real parent channel id for thread scope, so the cascade below is
    resolved against the parent that runtime will actually use, whatever the
    caller passed as channel_id.
    """
    if scope is ParticipationScope.WORKSPACE or scope_id is None:
        return None
    guild_id = _require_guild_id(auth)
    user_id = _require_discord_identity(auth)
    async with rest_client(_require_bot_token(runtime)) as client:
        _, member = await _resolve_member(client, guild_id, user_id)
        target = _require_guild_channel(await _resolve_channel(client, scope_id), guild_id)
        if scope is ParticipationScope.THREAD:
            if not isinstance(target, discord.Thread):
                raise ToolError("thread_id does not name a thread — pass a channel as channel_id")
            await _check_thread_view(client, target, member, user_id)
            return str(target.parent_id)
        if isinstance(target, discord.Thread):
            raise ToolError("channel_id names a thread — pass it as thread_id instead")
        _check_view_permission(target, member)
        return None
