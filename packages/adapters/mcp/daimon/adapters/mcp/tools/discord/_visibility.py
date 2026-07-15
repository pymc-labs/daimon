"""Discord channel and thread permission checks."""

from __future__ import annotations

import discord
from fastmcp.exceptions import ToolError


def _check_view_permission(  # pyright: ignore[reportUnusedFunction]
    channel: discord.abc.GuildChannel | discord.Thread, member: discord.Member
) -> None:
    # Admins and owners bypass channel overrides; in REST-only mode guild.owner
    # is unreliable, so check guild_permissions.administrator directly.
    if member.guild_permissions.administrator:
        return
    if not channel.permissions_for(member).view_channel:
        raise ToolError("missing view_channel permission")


def _check_send_permission(  # pyright: ignore[reportUnusedFunction]
    channel: discord.abc.GuildChannel | discord.Thread, member: discord.Member
) -> None:
    if member.guild_permissions.administrator:
        return
    perms = channel.permissions_for(member)
    if not perms.view_channel:
        raise ToolError("missing view_channel permission")
    if not perms.send_messages:
        raise ToolError("missing send_messages permission")


async def _ensure_thread_parent_cached(  # pyright: ignore[reportUnusedFunction]
    thread: discord.Thread,
) -> discord.TextChannel | discord.ForumChannel:
    """Fetch + cache the thread's parent channel when the REST-only client's
    guild channel cache doesn't have it. ``Thread.permissions_for`` raises
    ``ClientException('Parent channel not found')`` on an uncached parent, so
    every permission check on a thread must run through this first."""
    parent = thread.parent  # cached iff guild._add_channel was called
    if parent is not None:
        return parent
    if thread.parent_id is None:  # pyright: ignore[reportUnnecessaryComparison]  # runtime safety: deleted parent
        raise ToolError("thread parent channel not found")
    try:
        fetched = await thread.guild.fetch_channel(thread.parent_id)
    except (discord.NotFound, discord.Forbidden) as e:
        raise ToolError("thread parent channel not found or inaccessible") from e
    if not isinstance(fetched, (discord.TextChannel, discord.ForumChannel)):
        raise ToolError("thread parent is not a text channel")
    thread.guild._add_channel(fetched)  # pyright: ignore[reportPrivateUsage]
    return fetched


async def _check_thread_view(  # pyright: ignore[reportUnusedFunction]
    c: discord.Client, thread: discord.Thread, member: discord.Member, user_id: str
) -> None:
    """Caller may view a thread iff they can view the parent channel; private
    threads additionally require manage_threads or thread membership."""
    if member.guild_permissions.administrator:
        return
    parent = await _ensure_thread_parent_cached(thread)
    perms = parent.permissions_for(member)
    if not perms.view_channel:
        raise ToolError("missing view_channel permission")
    if thread.type is discord.ChannelType.private_thread and not perms.manage_threads:
        try:
            await thread.fetch_member(int(user_id))
        except discord.NotFound as e:
            raise ToolError("missing view_channel permission") from e
