"""Discord cleanup removes owned messages and preserves whole threads.

Owned means a tool post, or a turn's status card, answer or notice, or the
thread opened from a mention. A turn's posts also need the conversation
rights in tools/_tidy.py (`conversation_refusal`). Policy and ledger locks
are held through each bounded platform write. Shared ownership, limits and
audit rules live in tools/_tidy.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._tidy import (
    Check,
    PostRecord,
    TidyContext,
    TidyResult,
    conversation_refusal,
    policy_recheck,
    record_agent_posts,
    refuse,
    require_conversation_rights,
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
    ensure_application_id,
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._post_transport import (
    delete_own_message,
    edit_own_message,
    own_webhook_ids,
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_tidy import TidyOperation, TidyTarget
from daimon.core.stores.agent_posts import AgentPostRow, get_post, list_posts_in, mark_deleted
from daimon.core.stores.turn_origins import request_thread_archive
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
    requester_is_admin: bool


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
    return _Target(
        channel=channel,
        parent_id=parent_id,
        bot_user_id=c.user.id,
        requester_is_admin=member.guild_permissions.administrator,
    )


async def _fetch_own_bot_message(
    _client: discord.Client,
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
    if message.webhook_id is not None:
        await ensure_application_id(_client)
    # require_own_post has already checked the durable agent ledger. A deleted
    # webhook may no longer appear in the channel list, but its recorded post
    # is still the calling agent's.
    if not (
        (message.webhook_id is None and message.author.id == target.bot_user_id)
        or (
            message.webhook_id is not None
            and _client.application_id is not None
            and message.application_id == _client.application_id
        )
    ):
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


async def _auto_thread_opener(runtime: McpRuntime, ctx: TidyContext, target: _Target) -> str | None:
    """Who opened the target channel, if it is a thread this agent auto-opened.

    Only the caller's own agent's thread counts: opening a conversation with
    one agent gives no say over another agent's replies in it.
    """
    if target.parent_id is None:
        return None
    async with runtime.session_factory() as session:
        thread_post = await get_post(
            session,
            tenant_id=ctx.actor.tenant_id,
            platform=_PLATFORM,
            channel_id=target.parent_id,
            message_id=str(target.channel.id),
        )
    if (
        thread_post is None
        or thread_post.source != "auto_thread"
        or thread_post.agent_id != ctx.actor.agent_id
    ):
        return None
    return thread_post.requester_platform_user_id


async def _require_message_rights(
    runtime: McpRuntime,
    ctx: TidyContext,
    target: _Target,
    post: AgentPostRow,
    *,
    tool_name: str,
    operation: TidyOperation,
) -> None:
    if post.source == "tool":
        return
    await require_conversation_rights(
        runtime,
        ctx,
        post,
        tool_name=tool_name,
        operation=operation,
        requester_is_admin=target.requester_is_admin,
        thread_opener_id=await _auto_thread_opener(runtime, ctx, target),
    )


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
        await _require_message_rights(
            runtime, ctx, target, post, tool_name=tool, operation=operation
        )
        message = await _fetch_own_bot_message(
            c, runtime, ctx, target, tool_name=tool, operation=operation, message_id=message_id
        )
        replacement: discord.Message | None = None
        extra_messages: list[discord.Message] = []

        async def act() -> None:
            nonlocal replacement
            if post.source == "turn":
                # A status card is an embed with buttons, and an answer may
                # carry rendered table images: replacing only the text would
                # leave the stale card or tables showing under it.
                replacement = await edit_own_message(
                    c,
                    target.channel,
                    message,
                    extra_messages=extra_messages,
                    content=content,
                    embeds=[],
                    attachments=[],
                    view=None,
                )
            else:
                replacement = await edit_own_message(
                    c, target.channel, message, extra_messages=extra_messages, content=content
                )

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
        if replacement is not None:
            await record_agent_posts(
                runtime,
                auth,
                platform=_PLATFORM,
                posts=[
                    PostRecord(
                        channel_id=resolved_channel_id,
                        message_id=str(sent.id),
                        parent_channel_id=target.parent_id,
                        content=sent.content,
                    )
                    for sent in [replacement, *extra_messages]
                ],
            )
            async with runtime.session_factory.begin() as session:
                await mark_deleted(session, post_ids=[post.id], now=datetime.now(UTC))
            message_id = str(replacement.id)
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
        await _require_message_rights(
            runtime, ctx, target, post, tool_name=tool, operation=operation
        )
        message = await _fetch_own_bot_message(
            c, runtime, ctx, target, tool_name=tool, operation=operation, message_id=message_id
        )

        async def act() -> None:
            await delete_own_message(c, target.channel, message)

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
    # A thread opened from someone's mention is their conversation: only they
    # or a server admin may have it archived or cleared.
    await require_conversation_rights(
        runtime,
        ctx,
        thread_post,
        tool_name=tool_name,
        operation=operation,
        requester_is_admin=target.requester_is_admin,
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
    posting in it reopens it.

    The thread this turn is running in is archived when the turn ends, not
    now: Discord refuses edits in an archived thread, so the turn could not
    finish its own status card. The call is checked, counted and audited as
    usual, and only once every check has passed is the request written on the
    turn's origin (`request_thread_archive`), which the Discord adapter reads
    when the turn's run ends. A turn that fails leaves the thread open.
    """
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

        scheduled = ctx.origin is not None and ctx.origin.channel_id == str(thread.id)

        async def act() -> None:
            if not scheduled:
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
    result = TidyResult(
        platform=_PLATFORM,
        channel_id=target.parent_id or "",
        message_id=str(thread.id),
        action="archive_scheduled" if scheduled else "archived",
    )
    if scheduled and ctx.origin_context_id is not None:
        async with runtime.session_factory.begin() as session:
            recorded = await request_thread_archive(
                session,
                origin_id=uuid.UUID(ctx.origin_context_id),
                thread_id=str(thread.id),
                now=datetime.now(UTC),
            )
        if not recorded:
            raise ToolError("this turn has ended, so the thread was not archived. Do not retry.")
    return result


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
        target, thread, thread_post = await _resolve_own_thread(
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
        own_hook_ids = await own_webhook_ids(c, target.channel)
        opener = (
            thread_post.requester_platform_user_id if thread_post.source == "auto_thread" else None
        )
        for message in messages:
            post = owned.get(str(message.id))
            if post is None:
                continue
            if not (
                (message.webhook_id is None and message.author.id == target.bot_user_id)
                or (
                    message.webhook_id in own_hook_ids
                    and c.application_id is not None
                    and message.application_id == c.application_id
                )
            ):
                continue
            # A running turn's card (this turn's own included) and replies to
            # someone the caller may not speak for are left in place.
            if await conversation_refusal(
                runtime,
                ctx,
                post,
                requester_is_admin=target.requester_is_admin,
                thread_opener_id=opener,
            ):
                continue
            try:
                await run_action(
                    runtime,
                    ctx,
                    tool_name="delete_thread",
                    operation="thread.delete",
                    target=TidyTarget(str(thread.id), str(message.id), post.content_hmac),
                    checks=[_recheck(runtime, ctx, auth, target)],
                    act=lambda message=message: delete_own_message(c, target.channel, message),
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
