"""Discord tidy tools: edit or delete the agent's own messages, archive or
delete its own threads.

Provides: _edit_message_impl, _delete_message_impl, _archive_thread_impl,
_delete_thread_impl. The ownership, limit and audit rules live in
``tools/_tidy.py``. Order per call: resolve the turn, resolve the caller and
the target, the caller's own post permission, the escalation-channel guard,
the tenant write guard (`authorize(POST)`), the seal, the ledger, the bot
authorship check, then the audit row and only then the Discord call.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._tidy import (
    TidyContext,
    TidyResult,
    begin_action,
    finish_delete,
    finish_edit,
    record_failure,
    refuse,
    require_not_escalation_channel,
    require_own_post,
    resolve_tidy_context,
)
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_tidy import TidyOperation, TidyTarget
from daimon.core.stores.agent_posts import AgentPostRow, list_posts_in
from fastmcp.exceptions import ToolError

_PLATFORM = "discord"
_MAX_CONTENT_CHARS = 2000
# A thread larger than this is not deleted by a tool call; archive it instead.
_MAX_THREAD_MESSAGES = 50


@dataclass(frozen=True)
class _Target:
    channel: discord.abc.GuildChannel | discord.Thread
    parent_id: str | None
    bot_user_id: int


async def _resolve_target(
    c: discord.Client,
    runtime: McpRuntime,
    ctx: TidyContext,
    auth: AuthIdentity,
    *,
    channel_id: str,
) -> _Target:
    guild_id = _require_guild_id(auth)
    _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
    channel = _require_guild_channel(await _resolve_channel(c, channel_id), guild_id)
    parent_id: str | None = None
    if isinstance(channel, discord.Thread):
        parent_id = str((await _ensure_thread_parent_cached(channel)).id)
    _check_send_permission(channel, member)
    require_not_escalation_channel(runtime, channel_id=str(channel.id), parent_channel_id=parent_id)
    await _require_discord_channel_writable(runtime, auth, channel, origin=ctx.origin)
    ctx.read_policy.require(str(channel.id), parent_id)
    if c.user is None:
        raise ToolError("internal: discord client has no user")
    return _Target(channel=channel, parent_id=parent_id, bot_user_id=c.user.id)


async def _fetch_own_bot_message(
    runtime: McpRuntime,
    ctx: TidyContext,
    target: _Target,
    *,
    tool_name: str,
    operation: TidyOperation,
    message_id: str,
) -> discord.Message:
    if not isinstance(target.channel, discord.abc.Messageable):
        raise ToolError("channel does not hold messages")
    try:
        message = await target.channel.fetch_message(int(message_id))
    except discord.NotFound as e:
        raise ToolError("message not found") from e
    if message.author.id != target.bot_user_id or message.webhook_id is not None:
        raise await refuse(
            runtime,
            ctx,
            tool_name=tool_name,
            operation=operation,
            reason="not_bot_author",
            target=TidyTarget(channel_id=str(target.channel.id), message_id=message_id),
        )
    return message


def _message_id(message_id: str) -> str:
    if not message_id.isdigit():
        raise ToolError("message_id must be a Discord message id")
    return message_id


async def _edit_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    content: str,
    origin_context_id: str | None,
) -> TidyResult:
    message_id = _message_id(message_id)
    if not content.strip():
        raise ToolError("content must not be empty; use delete_message to remove a message")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(f"content is over Discord's {_MAX_CONTENT_CHARS}-character limit")
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    token = _require_bot_token(runtime)
    tool, operation = "edit_message", "message.edit"
    async with rest_client(token) as c:
        target = await _resolve_target(c, runtime, ctx, auth, channel_id=channel_id)
        resolved_channel_id = str(target.channel.id)
        post = await require_own_post(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            channel_id=resolved_channel_id,
            message_id=message_id,
        )
        message = await _fetch_own_bot_message(
            runtime, ctx, target, tool_name=tool, operation=operation, message_id=message_id
        )
        audit_target = TidyTarget(
            channel_id=resolved_channel_id,
            message_id=message_id,
            content_sha256=post.content_sha256,
        )
        await begin_action(runtime, ctx, tool_name=tool, operation=operation, target=audit_target)
        try:
            await message.edit(content=content)
        except discord.HTTPException as e:
            await record_failure(
                runtime,
                ctx,
                tool_name=tool,
                operation=operation,
                target=audit_target,
                reason="platform_refused",
            )
            raise ToolError(f"discord refused the edit: {e.text}") from e
    await finish_edit(runtime, post=post, content=content)
    return TidyResult(
        platform=_PLATFORM, channel_id=resolved_channel_id, message_id=message_id, action="edited"
    )


async def _delete_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    message_id = _message_id(message_id)
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    token = _require_bot_token(runtime)
    tool, operation = "delete_message", "message.delete"
    async with rest_client(token) as c:
        target = await _resolve_target(c, runtime, ctx, auth, channel_id=channel_id)
        resolved_channel_id = str(target.channel.id)
        post = await require_own_post(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            channel_id=resolved_channel_id,
            message_id=message_id,
        )
        message = await _fetch_own_bot_message(
            runtime, ctx, target, tool_name=tool, operation=operation, message_id=message_id
        )
        audit_target = TidyTarget(
            channel_id=resolved_channel_id,
            message_id=message_id,
            content_sha256=post.content_sha256,
        )
        await begin_action(runtime, ctx, tool_name=tool, operation=operation, target=audit_target)
        try:
            await message.delete()
        except discord.HTTPException as e:
            await record_failure(
                runtime,
                ctx,
                tool_name=tool,
                operation=operation,
                target=audit_target,
                reason="platform_refused",
            )
            raise ToolError(f"discord refused the delete: {e.text}") from e
    await finish_delete(runtime, post_ids=[post.id])
    return TidyResult(
        platform=_PLATFORM,
        channel_id=resolved_channel_id,
        message_id=message_id,
        action="deleted",
        messages_deleted=1,
    )


async def _resolve_own_thread(
    c: discord.Client,
    runtime: McpRuntime,
    ctx: TidyContext,
    auth: AuthIdentity,
    *,
    thread_id: str,
    tool_name: str,
    operation: TidyOperation,
) -> tuple[_Target, discord.Thread, AgentPostRow]:
    target = await _resolve_target(c, runtime, ctx, auth, channel_id=thread_id)
    thread = target.channel
    if not isinstance(thread, discord.Thread):
        raise ToolError(f"not a thread: {tool_name} takes a thread id")
    thread_post = await require_own_post(
        runtime,
        ctx,
        tool_name=tool_name,
        operation=operation,
        channel_id=target.parent_id or "",
        message_id=str(thread.id),
        kind="thread",
    )
    if thread.owner_id != target.bot_user_id:
        raise await refuse(
            runtime,
            ctx,
            tool_name=tool_name,
            operation=operation,
            reason="not_bot_author",
            target=TidyTarget(channel_id=str(thread.id), message_id=str(thread.id)),
        )
    return target, thread, thread_post


async def _archive_thread_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    """Archive a thread this agent opened. Its messages stay readable; anyone
    posting in it reopens it."""
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    token = _require_bot_token(runtime)
    tool, operation = "archive_thread", "thread.archive"
    async with rest_client(token) as c:
        target, thread, _ = await _resolve_own_thread(
            c, runtime, ctx, auth, thread_id=thread_id, tool_name=tool, operation=operation
        )
        audit_target = TidyTarget(channel_id=target.parent_id or "", message_id=str(thread.id))
        await begin_action(runtime, ctx, tool_name=tool, operation=operation, target=audit_target)
        try:
            await thread.edit(archived=True)
        except discord.HTTPException as e:
            await record_failure(
                runtime,
                ctx,
                tool_name=tool,
                operation=operation,
                target=audit_target,
                reason="platform_refused",
            )
            raise ToolError(f"discord refused the archive: {e.text}") from e
    return TidyResult(
        platform=_PLATFORM,
        channel_id=target.parent_id or "",
        message_id=str(thread.id),
        action="archived",
    )


async def _delete_thread_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    origin_context_id: str | None,
) -> TidyResult:
    """Delete a thread this agent opened, only while every message in it is
    this agent's own post (or a Discord system notice daimon's own actions
    produced). A thread anyone else wrote in is archived instead."""
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    token = _require_bot_token(runtime)
    tool, operation = "delete_thread", "thread.delete"
    async with rest_client(token) as c:
        target, thread, thread_post = await _resolve_own_thread(
            c, runtime, ctx, auth, thread_id=thread_id, tool_name=tool, operation=operation
        )
        thread_ref = TidyTarget(channel_id=target.parent_id or "", message_id=str(thread.id))
        messages = [m async for m in thread.history(limit=_MAX_THREAD_MESSAGES + 1)]
        if len(messages) > _MAX_THREAD_MESSAGES:
            raise ToolError(
                f"this thread has more than {_MAX_THREAD_MESSAGES} messages; "
                "archive_thread it instead"
            )
        async with runtime.session_factory() as session:
            own = await list_posts_in(
                session,
                tenant_id=ctx.actor.tenant_id,
                platform=_PLATFORM,
                channel_id=str(thread.id),
                message_ids=[str(m.id) for m in messages],
            )
        own = [p for p in own if p.agent_id == ctx.actor.agent_id]
        own_ids = {p.message_id for p in own}
        for m in messages:
            notice = m.author.id == target.bot_user_id and m.is_system()
            if str(m.id) not in own_ids and not notice:
                raise ToolError(
                    "this thread has messages that are not yours, so it cannot be "
                    "deleted. Use archive_thread instead, or delete_message on your "
                    "own messages."
                )
        await begin_action(runtime, ctx, tool_name=tool, operation=operation, target=thread_ref)
        try:
            await thread.delete()
        except discord.HTTPException as e:
            await record_failure(
                runtime,
                ctx,
                tool_name=tool,
                operation=operation,
                target=thread_ref,
                reason="platform_refused",
            )
            raise ToolError(f"discord refused the delete: {e.text}") from e
    await finish_delete(runtime, post_ids=[thread_post.id, *(p.id for p in own)])
    return TidyResult(
        platform=_PLATFORM,
        channel_id=target.parent_id or "",
        message_id=str(thread.id),
        action="deleted",
        messages_deleted=len([m for m in messages if str(m.id) in own_ids]),
    )
