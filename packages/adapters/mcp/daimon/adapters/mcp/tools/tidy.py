"""Channel tidy tools: edit_message, delete_message, archive_thread, delete_thread.

Registered beside the channel tools, with the same per-platform dispatch.
Teams has edit_message and delete_message only. Rules: ``tools/_tidy.py``.
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
from daimon.adapters.mcp.tools.teams._tidy import (
    _teams_delete_message_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_edit_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def register_tidy_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def edit_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
        content: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Replace the text of a message you posted with send_message or create_thread.

        Use it to correct or update your own post instead of posting again.
        Only your own posts: a person's message, another agent's, another
        bot's, or your normal chat replies are refused. channel_id is where
        the message is (Discord: the thread id for a message in a thread;
        Slack: the channel id, and message_id is the message ts; Teams: the
        conversation_id and activity_id send_message returned). Pass this
        turn's origin_context_id. Limited to 10 edits or deletes per turn and
        40 per hour. Refused in protected channels, channels you may not post
        in, sealed channels outside their own conversations and the support
        escalation channel.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            return await _teams_edit_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                content=content,
                origin_context_id=origin_context_id,
            )
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

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def delete_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
        origin_context_id: str | None = None,
    ) -> TidyResult:
        """Delete one message you posted with send_message or create_thread.

        Use it for your own stale or failed posts, one message per call.
        The same rules as edit_message: only your own posts, pass
        origin_context_id, the same limits and the same refused channels.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            return await _teams_delete_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                origin_context_id=origin_context_id,
            )
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
        """Archive a Discord thread you opened with create_thread.

        Its messages stay readable and a new post reopens it. Use it for a
        finished or abandoned thread of yours that others wrote in. Pass
        origin_context_id. Discord-only: Slack threads cannot be archived.
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
        """Remove your own messages from a thread you opened with create_thread.

        Discord keeps the thread and everyone else's messages. Slack refuses
        a thread with other people's replies. Reads at most 50 messages.
        Each deletion is checked, audited and counted against the 10 per turn
        and 40 per hour limits. A refusal stops with the number deleted.
        Discord: thread_id is the thread id. Slack: channel_id:thread_ts.
        Pass this turn's origin_context_id.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            raise ToolError(
                "delete_thread is not supported on Teams; delete your own posts there "
                "with delete_message"
            )
        if auth.platform == "slack":
            return await _slack_delete_thread_impl(
                runtime, auth, thread_id=thread_id, origin_context_id=origin_context_id
            )
        return await _delete_thread_impl(
            runtime, auth, thread_id=thread_id, origin_context_id=origin_context_id
        )
