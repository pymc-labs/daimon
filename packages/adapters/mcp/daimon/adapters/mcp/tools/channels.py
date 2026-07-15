"""Shared channel MCP tools with per-platform dispatch.

One registration serves both platforms: ``auth.platform == "slack"`` routes to
``tools/slack/`` impls, anything else to ``tools/discord/`` impls (which raise
their own identity errors for non-discord callers). Slack-unsupported tools
raise a uniform ToolError.
"""

from __future__ import annotations

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import (
    ChannelRow,
    MessageRow,
    ParsedLink,
    ReadThreadResult,
    SearchResult,
    ThreadRow,
    _get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _list_threads_impl,  # pyright: ignore[reportPrivateUsage]
    _parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _search_messages_impl,  # pyright: ignore[reportPrivateUsage]
    _send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._models import (
    SlackChannelRow,
    SlackMessageRow,
    SlackSearchResult,
    SlackThreadResult,
)
from daimon.adapters.mcp.tools.slack._read import (
    _slack_get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._search import (
    _slack_search_messages_impl,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _slack_unsupported(tool_name: str) -> ToolError:
    raise ToolError(f"{tool_name} is not supported on Slack yet")


def register_channel_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_channels(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> list[ChannelRow] | list[SlackChannelRow]:
        """List channels in this server/workspace that you can view."""
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return await _slack_list_channels_impl(runtime, auth)
        return await _list_channels_impl(runtime, auth)

    @mcp.tool
    async def read_channel(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        limit: int = 50,
    ) -> list[MessageRow] | list[SlackMessageRow]:
        """Read recent messages from a channel, oldest-first.

        For threads use read_thread.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return await _slack_read_channel_impl(runtime, auth, channel_id=channel_id, limit=limit)
        return await _read_channel_impl(runtime, auth, channel_id=channel_id, limit=limit)

    @mcp.tool
    async def read_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> ReadThreadResult | SlackThreadResult:
        """Read messages from a thread, oldest-first.

        Discord: thread_id is the thread's channel id; use before for older messages.
        Slack: thread_id is channel_id:thread_ts (e.g. C0123456789:1717171717.123456);
        before is ignored.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return await _slack_read_thread_impl(runtime, auth, thread_id=thread_id, limit=limit)
        return await _read_thread_impl(
            runtime, auth, thread_id=thread_id, limit=limit, before=before
        )

    @mcp.tool
    async def get_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
    ) -> MessageRow | SlackMessageRow:
        """Fetch a single message by channel and message id (Slack: the message ts)."""
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return await _slack_get_message_impl(
                runtime, auth, channel_id=channel_id, message_id=message_id
            )
        return await _get_message_impl(runtime, auth, channel_id=channel_id, message_id=message_id)

    @mcp.tool
    async def list_threads(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
    ) -> list[ThreadRow]:
        """List active and archived public threads for a channel.

        Archived private threads are not listed.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("list_threads")
        return await _list_threads_impl(runtime, auth, channel_id=channel_id)

    @mcp.tool
    async def parse_link(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        url: str,
    ) -> ParsedLink:
        """Extract IDs from a Discord URL.

        Supports discord.com, ptb.discord.com, and canary.discord.com URLs.

        After parsing:
        - If link_type is "channel": use read_channel(channel_id)
        - If link_type is "message_or_thread": try read_thread(thread_id) first;
          if read_thread fails, it is a message: use get_message(channel_id, message_id)
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("parse_link")
        return _parse_link_impl(url, caller_guild_id=auth.external_id)

    @mcp.tool
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        content: str,
        attachments: list[dict[str, str]] | None = None,
        file_handles: list[str] | None = None,
    ) -> MessageRow:
        """Post a message to a Discord channel.

        Only call this when the user explicitly asks you to send or post something
        to Discord. Do NOT call it to share results, summaries, or outputs unless
        the user specifically requested a Discord post. Deliver output in your reply
        instead.

        ``attachments=[{url, filename}]`` fetches over https (<=25 MiB each).
        ``file_handles=[filename, ...]`` references files previously stored
        in the media FileStore by ``generate_audio`` or ``generate_image``.
        Combined cap of 10 attachments per message.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("send_message")
        return await _send_message_impl(
            runtime,
            auth,
            channel_id=channel_id,
            content=content,
            attachments=attachments,
            file_handles=file_handles,
        )

    _VALID_AUTHOR_TYPES = frozenset({"user", "bot", "webhook"})
    _VALID_HAS = frozenset({"image", "video", "file", "sticker", "embed", "link", "poll", "sound"})

    @mcp.tool
    async def search_messages(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        content: str | None = None,
        channel_ids: list[str] | None = None,
        author_ids: list[str] | None = None,
        author_types: list[str] | None = None,
        mentions: list[str] | None = None,
        has: list[str] | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResult | SlackSearchResult:
        """Search messages with server-side filters.

        Limit caps at 25 per page — paginate with offset. When the search is
        scoped to channel_ids, total_results is the exact count for those
        channels (you must be able to view them; they are rejected before
        searching). Unscoped searches report only the visible rows — the
        guild-wide count is withheld because it includes channels you cannot
        view. Slack: only content + limit are supported (other filters are
        Discord-only), and 1:1 DM hits are only returned in a DM with daimon.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            if content is None:
                raise ToolError("slack search requires a content query")
            if any((channel_ids, author_ids, author_types, mentions, has, offset)):
                raise ToolError(
                    "slack search supports only content and limit — other filters are Discord-only"
                )
            return await _slack_search_messages_impl(runtime, auth, content=content, limit=limit)
        # Validate enum-typed params at the tool boundary (shell) so the impl
        # receives the precise Literal types without type-ignore suppressions.
        if author_types and any(a not in _VALID_AUTHOR_TYPES for a in author_types):
            raise ToolError(f"invalid author_type; valid: {sorted(_VALID_AUTHOR_TYPES)}")
        if has and any(h not in _VALID_HAS for h in has):
            raise ToolError(f"invalid has value; valid: {sorted(_VALID_HAS)}")
        return await _search_messages_impl(
            runtime,
            auth,
            content=content,
            channel_ids=channel_ids,
            author_ids=author_ids,
            author_types=author_types,  # type: ignore[arg-type]  # validated above
            mentions=mentions,
            has=has,  # type: ignore[arg-type]  # validated above
            limit=limit,
            offset=offset,
        )
