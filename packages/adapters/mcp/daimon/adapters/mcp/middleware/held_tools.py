"""The tools a held turn may reach, and nothing else.

A held turn (`daimon.core.turn_hold.TurnHold`) is kept to one channel and its
threads and carries no administrator authority. Its credential therefore sees an
explicit allowlist of Daimon's tools: everything else is hidden from
`tools/list` and `search_tools`, and refused with a plain error if a client
calls it by name. A tool added later is closed by default until someone adds it
here, with a reason, in review.

Allowlisted tools are not trusted on the strength of being listed. The channel
tools apply the hold through the channel policy themselves; the entries here are
only the ones that either do that or touch nothing outside the conversation.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastmcp.server.context import Context
from fastmcp.server.transforms.visibility import disable_components, enable_components

#: The search transform's two proxies. They carry no data of their own: a
#: proxied call re-enters the identity middleware under the inner tool's name,
#: where `held_tool_refusal` judges it.
HELD_PROXY_TOOLS: Final = frozenset({"search_tools", "call_tool"})

HELD_TOOL_ALLOWLIST: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Channel tools: they authorize against the channel policy, which applies
        # the hold, so a held turn reads and posts only inside its channel.
        "list_channels": "channel listing, filtered by the channel policy",
        "read_channel": "reads channel messages through the channel policy",
        "read_thread": "reads a thread through the channel policy",
        "get_message": "reads one message through the channel policy",
        "list_threads": "lists a channel's threads through the channel policy",
        "search_messages": "searches messages through the channel policy",
        "parse_link": "pure link parsing; reads nothing",
        "send_message": "posts to a channel through the channel policy",
        "create_thread": "opens a thread under a channel through the channel policy",
        "rename_thread": "renames a thread the channel policy lets the caller post in",
        "edit_message": "edits only a message the agent itself posted",
        "delete_message": "deletes only a message the agent itself posted",
        "archive_thread": "archives only a thread the agent opened",
        "delete_thread": "removes only the agent's own messages from a thread it opened",
        # The conversation itself.
        "where_am_i": "describes the current place; applies the hold to what it lists",
        # Timers resume the same thread, as the person who asked.
        "create_timer": "resumes this same thread once, as the requester",
        "list_timers": "lists the caller's own timers",
        "cancel_timer": "cancels one of the caller's own timers",
        # The caller's own sessions and transcripts, read through the channel policy.
        "list_sessions": "lists the caller's own conversations",
        "get_session": "reads one of the caller's own conversations",
        "list_session_events": "reads the caller's own transcript through the read policy",
        # Publishing the conversation's own sandbox output waits for the requester's
        # approval card. These two only mint an upload URL for it: they create no
        # agent and no token. `publish_report` is not here: it publishes an agent's
        # reader, which copies the agent's prompt and skills and mints an agent token.
        "create_notebook_upload_url": "scratch notebook upload, only after the requester approves",
        "create_attachment_upload_url": "attachment upload, only after the requester approves",
        # Pure utilities.
        "now": "wall-clock time; reads nothing",
        "convert": "time-zone conversion; reads nothing",
    }
)


def held_tool_refusal(tool_name: str) -> str | None:
    """Why a held credential may not call `tool_name`, or None when it may."""
    if tool_name in HELD_TOOL_ALLOWLIST or tool_name in HELD_PROXY_TOOLS:
        return None
    return (
        "This conversation is limited to its own channel, so that tool isn't "
        "available here. Tell the person it can't be done from this conversation."
    )


async def narrow_to_held_tools(ctx: Context) -> None:
    """Hide every tool, resource and prompt but the allowlist (and the search proxies).

    Rules are per request, so a tool the allowlist loses between requests is gone
    on the next one. Nothing is enabled by tag, so no tag-based visibility
    (admin, agent-chat, platform) can widen the set.
    """
    await disable_components(ctx, match_all=True)
    await enable_components(ctx, names={*HELD_TOOL_ALLOWLIST, *HELD_PROXY_TOOLS})
