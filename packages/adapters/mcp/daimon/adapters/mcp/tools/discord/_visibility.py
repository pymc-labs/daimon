"""Discord channel and thread permission checks."""

from __future__ import annotations

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable
from daimon.core.authz import Place
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


def _bot_read_error(  # pyright: ignore[reportUnusedFunction]
    channel: discord.abc.GuildChannel | discord.Thread | str,
    *,
    missing_history: bool = False,
) -> ToolError:
    """Explain a Discord bot permission denial without exposing an HTTP error."""
    label = f"#{channel.name}" if not isinstance(channel, str) else f"#{channel}"
    verb = "read" if missing_history else "view"
    return ToolError(
        f"daimon's Discord role can't {verb} {label}; a server admin can grant "
        "View Channel and Read Message History"
    )


async def _bot_lacks_read_permission(  # pyright: ignore[reportUnusedFunction]
    guild: discord.Guild, channel: discord.abc.GuildChannel | discord.Thread
) -> str | None:
    """Check the bot's actual role permissions after an empty read."""
    me = guild._state.user  # pyright: ignore[reportPrivateUsage]
    if me is None:
        return None
    bot = await guild.fetch_member(me.id)
    if isinstance(channel, discord.Thread):
        parent = await _ensure_thread_parent_cached(channel)
        perms = parent.permissions_for(bot)
    else:
        perms = channel.permissions_for(bot)
    if not perms.view_channel:
        return "view"
    if not perms.read_message_history:
        return "history"
    return None


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


def _check_create_thread_permission(  # pyright: ignore[reportUnusedFunction]
    channel: discord.TextChannel | discord.ForumChannel, member: discord.Member
) -> None:
    """Discord gates thread creation differently by parent type. A forum post's
    starter message IS the post, so only send_messages is required. A text
    channel thread additionally needs send_messages_in_threads: the impl posts
    the starter message into the new thread on the caller's behalf, so the
    caller's reach into threads is what authorizes that."""
    if member.guild_permissions.administrator:
        return
    perms = channel.permissions_for(member)
    if not perms.view_channel:
        raise ToolError("missing view_channel permission")
    if isinstance(channel, discord.ForumChannel):
        if not perms.send_messages:
            raise ToolError("missing send_messages permission")
        return
    if not perms.create_public_threads:
        raise ToolError("missing create_public_threads permission")
    if not perms.send_messages_in_threads:
        raise ToolError("missing send_messages_in_threads permission")


def _check_rename_thread_permission(  # pyright: ignore[reportUnusedFunction]
    thread: discord.Thread, member: discord.Member, *, bot_user_id: int | None
) -> None:
    """Renaming someone else's thread is moderation, so it takes manage_threads
    exactly as Discord itself requires. A thread daimon opened for a chat is
    the caller's own conversation: being able to post in it is enough, and
    daimon, as the thread's owner, performs the edit on their behalf. Locked
    and archived are moderation state too, so they fall back to
    manage_threads even on daimon's threads. Requires the parent cached
    (``_ensure_thread_parent_cached``) first."""
    if member.guild_permissions.administrator:
        return
    perms = thread.permissions_for(member)
    if not perms.view_channel:
        raise ToolError("missing view_channel permission")
    if perms.manage_threads:
        return
    if bot_user_id is None or thread.owner_id != bot_user_id:
        raise ToolError(
            "missing manage_threads permission — only threads daimon opened can be "
            "renamed without it"
        )
    if thread.locked or thread.archived:
        raise ToolError("this thread is locked or archived — renaming it needs manage_threads")
    if not perms.send_messages_in_threads:
        raise ToolError("missing send_messages_in_threads permission")


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


async def _require_discord_channel_writable(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel: discord.abc.GuildChannel | discord.Thread,
    *,
    origin: Place | None = None,
) -> None:
    """The tenant write guard for a Discord target: the channel, the parent of a
    thread, and the category either sits in. A thread whose parent can't be
    resolved is refused by ``_ensure_thread_parent_cached``. Call after the
    caller-permission check; admins get no bypass here."""
    if isinstance(channel, discord.Thread):
        parent = await _ensure_thread_parent_cached(channel)
        await require_channel_writable(
            runtime,
            auth,
            channel_id=str(channel.id),
            parent_channel_id=str(parent.id),
            category_id=str(parent.category_id) if parent.category_id is not None else None,
            origin=origin,
        )
        return
    category_id = channel.category_id
    await require_channel_writable(
        runtime,
        auth,
        channel_id=str(channel.id),
        category_id=str(category_id) if category_id is not None else None,
        origin=origin,
    )
