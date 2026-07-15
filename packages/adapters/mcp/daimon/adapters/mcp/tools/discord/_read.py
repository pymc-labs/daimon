"""Discord read-tool implementations.

Provides: _read_channel_impl, _list_channels_impl, _get_message_impl,
_parse_link_impl, _read_thread_impl, _list_threads_impl.

Every impl follows the locked sequence:
  gates -> bound limit -> rest_client -> _resolve_member FIRST ->
  _resolve_channel -> _require_guild_channel -> permission check ->
  typed discord.py call -> map rows.
"""

from __future__ import annotations

import re
from typing import Literal

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
from daimon.adapters.mcp.tools.discord._models import (
    ChannelRow,  # pyright: ignore[reportPrivateUsage]
    MessageRow,  # pyright: ignore[reportPrivateUsage]
    ParsedLink,  # pyright: ignore[reportPrivateUsage]
    ReadThreadResult,  # noqa: F811  # pyright: ignore[reportPrivateUsage,reportUnusedImport]
    ThreadRow,  # noqa: F811  # pyright: ignore[reportPrivateUsage,reportUnusedImport]
    _to_message_row,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp.exceptions import ToolError

_MAX_HISTORY_LIMIT: int = 200

# Regex ported from the reference implementation (daimon_clients/discord/api.py).
# Matches discord.com, ptb.discord.com, canary.discord.com, discordapp.com.
_DISCORD_LINK_PATTERN = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(\d+)/(\d+)(?:/(\d+))?"
)


# ---------------------------------------------------------------------------
# read_channel
# ---------------------------------------------------------------------------


async def _read_channel_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    limit: int = 50,
) -> list[MessageRow]:
    """Read recent messages from a channel, oldest-first.

    If the resolved channel is a thread, raises ToolError directing to
    read_thread (keeps parse_link routing unambiguous).
    """
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)
    bounded_limit = max(1, min(limit, _MAX_HISTORY_LIMIT))

    async with rest_client(token) as c:
        _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)

        if isinstance(channel, discord.Thread):
            raise ToolError("this is a thread — use read_thread")

        _check_view_permission(channel, member)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support message history")
        page = [m async for m in channel.history(limit=bounded_limit)]
        return [_to_message_row(m) for m in reversed(page)]


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------


async def _list_channels_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[ChannelRow]:
    """List viewable channels in the caller's guild."""
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    async with rest_client(token) as c:
        guild, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        channels = await guild.fetch_channels()
        result: list[ChannelRow] = []
        for ch in channels:
            if member.guild_permissions.administrator or ch.permissions_for(member).view_channel:
                result.append(
                    ChannelRow(
                        id=str(ch.id),
                        name=ch.name,
                        type=ch.type.name,
                        category_id=str(ch.category_id) if ch.category_id else None,
                    )
                )
        return result


# ---------------------------------------------------------------------------
# get_message
# ---------------------------------------------------------------------------


async def _get_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
) -> MessageRow:
    """Fetch a single message by id.

    Thread-aware: uses _check_thread_view for thread channels.
    """
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    async with rest_client(token) as c:
        _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)

        if isinstance(channel, discord.Thread):
            await _check_thread_view(c, channel, member, _require_discord_identity(auth))
        else:
            _check_view_permission(channel, member)

        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support message history")
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound as e:
            raise ToolError("message not found") from e
        return _to_message_row(message)


# ---------------------------------------------------------------------------
# parse_link (pure — no I/O)
# ---------------------------------------------------------------------------


def _parse_link_impl(  # pyright: ignore[reportUnusedFunction]
    url: str,
    *,
    caller_guild_id: str | None = None,
) -> ParsedLink:
    """Parse a Discord URL into structured components.

    Pure function — no network calls. Routing hint references
    read_channel/read_thread/get_message.
    """
    match = _DISCORD_LINK_PATTERN.match(url)
    if not match:
        raise ToolError("not a recognized discord channel or message link")

    guild_id = match.group(1)
    channel_id = match.group(2)
    message_id = match.group(3)  # None when not present

    if message_id is not None:
        link_type: Literal["channel", "message_or_thread"] = "message_or_thread"
        hint = (
            f'try read_thread(thread_id="{message_id}") first — '
            f"Discord thread URLs put the thread id in the third segment; "
            f"if read_thread fails, it is a message: "
            f'get_message(channel_id="{channel_id}", message_id="{message_id}")'
        )
    else:
        link_type = "channel"
        hint = f'use read_channel(channel_id="{channel_id}")'

    if caller_guild_id is not None and guild_id != caller_guild_id:
        hint += (
            " — note: this link points at a different server; "
            "these tools only read the caller's guild"
        )

    return ParsedLink(
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
        link_type=link_type,
        hint=hint,
    )


# ---------------------------------------------------------------------------
# read_thread
# ---------------------------------------------------------------------------


async def _read_thread_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    limit: int = 50,
    before: str | None = None,
) -> ReadThreadResult:
    """Read messages from a thread, oldest-first, with before-cursor pagination.

    Returns a ReadThreadResult with rows (oldest-first), next_before cursor,
    and a hint when more messages are available.
    """
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)
    bounded_limit = max(1, min(limit, _MAX_HISTORY_LIMIT))

    async with rest_client(token) as c:
        _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_channel = await _resolve_channel(c, thread_id)
        channel = _require_guild_channel(raw_channel, guild_id)

        if not isinstance(channel, discord.Thread):
            raise ToolError("not a thread — use read_channel for channels")

        await _check_thread_view(c, channel, member, _require_discord_identity(auth))

        before_obj = discord.Object(id=int(before)) if before is not None else None
        page = [m async for m in channel.history(limit=bounded_limit, before=before_obj)]
        rows = [_to_message_row(m) for m in reversed(page)]
        next_before = str(page[-1].id) if len(page) == bounded_limit else None
        hint = (
            f"more messages available — pass before={next_before} to read older messages"
            if next_before is not None
            else None
        )
        return ReadThreadResult(rows=rows, next_before=next_before, hint=hint)


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


async def _list_threads_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
) -> list[ThreadRow]:
    """List active and archived public threads for a parent channel.

    Merges active threads (filtered by parent_id) with archived public threads.
    Private threads (type 12) are included only if the caller passes the
    membership rule; silently omitted otherwise.
    """
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    async with rest_client(token) as c:
        guild, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_parent = await _resolve_channel(c, channel_id)
        parent = _require_guild_channel(raw_parent, guild_id)

        if isinstance(parent, discord.Thread):
            raise ToolError("not a channel — list_threads takes a parent channel id")
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            raise ToolError("channel does not support threads")

        _check_view_permission(parent, member)

        # Active threads — guild-wide, filter by parent_id
        active_threads = await guild.active_threads()
        result: list[ThreadRow] = []
        for t in active_threads:
            if t.parent_id != int(channel_id):
                continue
            # Private threads: check membership/manage_threads
            if t.type is discord.ChannelType.private_thread:
                try:
                    await _check_thread_view(c, t, member, _require_discord_identity(auth))
                except ToolError:
                    continue  # silently omit (no existence leak)
            result.append(_to_thread_row(t))

        # Archived public threads (private=False is default)
        async for t in parent.archived_threads(limit=50):
            result.append(_to_thread_row(t))

        return result


def _to_thread_row(t: discord.Thread) -> ThreadRow:
    """Map a discord.Thread to a ThreadRow."""
    if t.last_message_id is not None:
        last_activity = discord.utils.snowflake_time(t.last_message_id).isoformat()
    elif t.archived:
        last_activity = t.archive_timestamp.isoformat()
    else:
        last_activity = discord.utils.snowflake_time(t.id).isoformat()
    return ThreadRow(
        id=str(t.id),
        name=t.name,
        parent_id=str(t.parent_id),
        archived=t.archived,
        message_count=t.message_count,
        last_activity=last_activity,
    )
