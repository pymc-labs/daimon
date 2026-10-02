"""Discord cleanup removes owned messages and preserves whole threads.

Policy and ledger locks are held through each bounded platform write.
Shared ownership, limits and audit rules live in tools/_tidy.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._tidy import (
    Check,
    TidyContext,
    TidyResult,
    policy_recheck,
    refuse,
    require_not_escalation_channel,
    require_own_post,
    resolve_tidy_context,
    run_action,
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


def _recheck(runtime: McpRuntime, ctx: TidyContext, auth: AuthIdentity, target: _Target) -> Check:
    channel = target.channel

    return policy_recheck(
        channel_id=str(channel.id),
        parent_channel_id=target.parent_id,
        category_id=str(channel.category_id) if channel.category_id else None,
        sealed=[(str(channel.id), target.parent_id)],
    )


def _describe(verb: str) -> Callable[[Exception], ToolError | None]:
    def describe(exc: Exception) -> ToolError | None:
        if isinstance(exc, discord.HTTPException):
            return ToolError(f"discord refused the {verb}: {exc.text}")
        return None

    return describe


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
    tool: str = "edit_message"
    operation: TidyOperation = "message.edit"
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

        async def act() -> None:
            await message.edit(content=content)

        await run_action(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            target=TidyTarget(
                channel_id=resolved_channel_id,
                message_id=message_id,
                content_hmac=post.content_hmac,
            ),
            checks=[_recheck(runtime, ctx, auth, target)],
            act=act,
            describe_error=_describe("edit"),
            post=post,
            content=content,
        )
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
    tool: str = "delete_message"
    operation: TidyOperation = "message.delete"
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

        async def act() -> None:
            await message.delete()

        await run_action(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            target=TidyTarget(
                channel_id=resolved_channel_id,
                message_id=message_id,
                content_hmac=post.content_hmac,
            ),
            checks=[_recheck(runtime, ctx, auth, target)],
            act=act,
            describe_error=_describe("delete"),
            post=post,
        )
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
    tool: str = "archive_thread"
    operation: TidyOperation = "thread.archive"
    async with rest_client(token) as c:
        target, thread, thread_post = await _resolve_own_thread(
            c, runtime, ctx, auth, thread_id=thread_id, tool_name=tool, operation=operation
        )

        async def act() -> None:
            await thread.edit(archived=True)

        await run_action(
            runtime,
            ctx,
            tool_name=tool,
            operation=operation,
            target=TidyTarget(channel_id=target.parent_id or "", message_id=str(thread.id)),
            checks=[_recheck(runtime, ctx, auth, target)],
            act=act,
            describe_error=_describe("archive"),
            post=thread_post,
        )
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
    """Delete only recorded own messages. Keep the thread and all other posts."""
    ctx = await resolve_tidy_context(
        runtime, auth, platform=_PLATFORM, origin_context_id=origin_context_id
    )
    deleted = 0
    async with rest_client(_require_bot_token(runtime)) as c:
        target, thread, _ = await _resolve_own_thread(
            c,
            runtime,
            ctx,
            auth,
            thread_id=thread_id,
            tool_name="delete_thread",
            operation="thread.delete",
        )
        messages = [m async for m in thread.history(limit=_MAX_THREAD_MESSAGES + 1)]
        if len(messages) > _MAX_THREAD_MESSAGES:
            raise ToolError("this thread has more than 50 messages; archive_thread it instead")
        async with runtime.session_factory() as session:
            own = await list_posts_in(
                session,
                tenant_id=ctx.actor.tenant_id,
                platform=_PLATFORM,
                channel_id=str(thread.id),
                message_ids=[str(m.id) for m in messages],
            )
        owned = {p.message_id: p for p in own if p.agent_id == ctx.actor.agent_id}
        for message in messages:
            post = owned.get(str(message.id))
            if post is None or message.author.id != target.bot_user_id or message.webhook_id:
                continue
            try:
                await run_action(
                    runtime,
                    ctx,
                    tool_name="delete_thread",
                    operation="thread.delete",
                    target=TidyTarget(str(thread.id), str(message.id), post.content_hmac),
                    checks=[_recheck(runtime, ctx, auth, target)],
                    act=message.delete,
                    describe_error=_describe("delete"),
                    post=post,
                )
            except ToolError as exc:
                raise ToolError(f"{deleted} messages deleted; stopped: {exc}") from exc
            deleted += 1
    return TidyResult(
        platform=_PLATFORM,
        channel_id=target.parent_id or "",
        message_id=thread_id,
        action="deleted",
        messages_deleted=deleted,
    )
