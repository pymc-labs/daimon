"""Channel tidy tools: edit_message, delete_message, archive_thread, delete_thread.

Registered beside the channel tools, with the same per-platform dispatch.
Discord and Slack only. Rules: ``tools/_tidy.py``.
"""

from __future__ import annotations

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._tidy import TidyResult
from daimon.adapters.mcp.tools.discord._tidy import (
    _archive_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_message_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _edit_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._tidy import (
    _slack_delete_message_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_delete_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_edit_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _teams_unsupported(tool_name: str) -> ToolError:
    return ToolError(f"{tool_name} is not supported on Teams yet")


def register_tidy_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def edit_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
        content: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Replace the text of a message you posted.

        Use it to correct or update your own post instead of posting again.
        Your own posts are those you sent with send_message or create_thread
        and, on Discord, your chat replies and status cards (a status card's
        embed is replaced by the new text). A person's message, another
        agent's or another bot's is refused. A reply or card is refused while
        its turn is still running, and unless the person asking started that
        turn, opened the thread, or is a server admin. channel_id is where
        the message is (Discord: the thread id for a message in a thread;
        Slack: the channel id, and message_id is the message ts). Pass this
        turn's origin_context_id. Limited to 10 edits or deletes per turn and
        40 per hour. Refused in protected channels, channels you may not post
        in, sealed channels outside their own conversations and the support
        escalation channel.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            raise _teams_unsupported("edit_message")
        if auth.platform == "slack":
            return await _slack_edit_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                content=content,
                origin_context_id=origin_context_id,
            )
        return await _edit_message_impl(
            runtime,
            auth,
            channel_id=channel_id,
            message_id=message_id,
            content=content,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def delete_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Delete one message you posted, or on Discord one of your replies or status cards.

        Use it for your own stale, empty or failed posts, one message per
        call. The same rules as edit_message: only your own posts and
        replies, pass origin_context_id, the same limits and the same refused
        channels.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            raise _teams_unsupported("delete_message")
        if auth.platform == "slack":
            return await _slack_delete_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                origin_context_id=origin_context_id,
            )
        return await _delete_message_impl(
            runtime,
            auth,
            channel_id=channel_id,
            message_id=message_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def archive_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Archive a Discord thread you opened, with create_thread or from a mention.

        Its messages stay readable and a new post reopens it. Use it for a
        finished or abandoned thread of yours that others wrote in. A thread
        opened from someone's mention can be archived only when that person
        or a server admin asks. Pass origin_context_id. Discord-only: Slack
        threads cannot be archived.
        """
        auth = await _auth(ctx)
        if auth.platform != "discord":
            raise ToolError("archive_thread is Discord-only")
        return await _archive_thread_impl(
            runtime, auth, thread_id=thread_id, origin_context_id=origin_context_id
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def delete_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Remove your own messages from a thread you opened.

        Discord: a thread from create_thread or from a mention (the latter
        only when the person who mentioned you or a server admin asks); your
        posts, replies and status cards go, except the running turn's.
        Discord keeps the thread and everyone else's messages. Slack refuses
        a thread with other people's replies. Reads at most 50 messages.
        Each deletion is checked, audited and counted against the 10 per turn
        and 40 per hour limits. A refusal stops with the number deleted.
        Discord: thread_id is the thread id. Slack: channel_id:thread_ts.
        Pass this turn's origin_context_id.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            raise _teams_unsupported("delete_thread")
        if auth.platform == "slack":
            return await _slack_delete_thread_impl(
                runtime, auth, thread_id=thread_id, origin_context_id=origin_context_id
            )
        return await _delete_thread_impl(
            runtime, auth, thread_id=thread_id, origin_context_id=origin_context_id
        )
