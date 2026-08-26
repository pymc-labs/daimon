"""Discord thread-creation implementation.

Provides: _create_thread_impl.

Follows the locked sequence from ``_read.py``'s module docstring: validate
inputs -> rest_client -> _resolve_member FIRST -> _resolve_channel ->
_require_guild_channel -> permission check -> typed discord.py call -> map
row. A freshly created thread reports message_count 0 and a last_activity
derived from its own snowflake if built via the shared ``_to_thread_row``
helper, so this module builds ``ThreadRow`` explicitly from the created
thread and its starter message instead.
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
from daimon.adapters.mcp.tools.discord._models import ThreadRow
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_create_thread_permission,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp.exceptions import ToolError

_MAX_NAME_CHARS = 100


def _validate_thread_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise ToolError("thread name must not be empty")
    if len(stripped) > _MAX_NAME_CHARS:
        raise ToolError(f"thread name must be at most {_MAX_NAME_CHARS} characters")
    return stripped


async def _create_thread_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    name: str,
    content: str,
) -> ThreadRow:
    """Create a public thread on a text channel, or a post on a forum channel.

    ``content`` is always posted as the thread's starter message. Text
    channels get an explicit public thread (never Discord's private-thread
    default); forum channels require a starter message to create a post at
    all, so the same ``content`` parameter serves both shapes.
    """
    validated_name = _validate_thread_name(name)
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    async with rest_client(token) as c:
        _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)

        if isinstance(channel, discord.Thread):
            raise ToolError("not a channel — create_thread takes a parent channel id")
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise ToolError("channel does not support threads")

        _check_create_thread_permission(channel, member)

        if isinstance(channel, discord.ForumChannel):
            try:
                thread, starter = await channel.create_thread(name=validated_name, content=content)
            except discord.Forbidden as e:
                raise ToolError(
                    "daimon is missing the send_messages permission needed to post in this forum"
                ) from e
        else:
            try:
                thread = await channel.create_thread(
                    name=validated_name, type=discord.ChannelType.public_thread
                )
                starter = await thread.send(content=content)
            except discord.Forbidden as e:
                raise ToolError(
                    "daimon is missing the permission needed to create this thread"
                ) from e

        return ThreadRow(
            id=str(thread.id),
            name=thread.name,
            parent_id=str(thread.parent_id),
            archived=thread.archived,
            message_count=1,
            last_activity=starter.created_at.isoformat(),
        )
