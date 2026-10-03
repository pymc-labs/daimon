"""The MCP tools a person from another organisation may call: an allow-list.

An external participant (a Teams shared channel's member from another
organisation: `accounts.is_external`, or a turn that could not tell) talks to
the agent inside one isolated channel. They may have it read, search and post
in that conversation (the isolation hold keeps those calls there), share files
into it, sign in to an MCP server the agent already has (`request_mcp_oauth`
checks that), set timers in the thread, start their own work there over, and
use tools that reveal nothing about the organisation's setup. Every other tool,
including any added later, is refused by `IdentityMiddleware` with the text
below, which the agent relays: no setup changes, keys, skills, environments,
agents, routines, public links (reports, notebooks) or direct messages.
"""

from __future__ import annotations

from typing import Final

EXTERNAL_ALLOWED_TOOLS: Final = frozenset(
    {
        # The conversation, held to the isolated channel.
        "create_thread",
        "get_message",
        "list_channels",
        "list_threads",
        "parse_link",
        "read_channel",
        "read_thread",
        "search_messages",
        "send_message",
        "post_wizard",
        "create_file_upload_url",
        "get_thread_participation",
        # Their own sessions and work in it.
        "get_session",
        "list_session_events",
        "list_sessions",
        "start_fresh_task",
        "cancel_timer",
        "create_timer",
        "list_timers",
        # Signing in to a server the agent already has.
        "request_mcp_oauth",
        # Nothing about the organisation.
        "convert",
        "fetch_youtube_transcript",
        "now",
    }
)


# The search transform's proxies: `call_tool` runs the middleware again with the
# inner tool's name, so the allow-list still decides what it reaches.
SEARCH_PROXY_TOOLS: Final = frozenset({"call_tool", "search_tools"})


def external_refusal(tool_name: str) -> str | None:
    """Why an external participant may not call `tool_name`, or None when they may."""
    if tool_name in EXTERNAL_ALLOWED_TOOLS or tool_name in SEARCH_PROXY_TOOLS:
        return None
    if tool_name == "send_direct_message":
        return (
            "The person asking is from another organisation, so daimon messages no one "
            "directly for them. Nothing was sent. Answer them in this conversation."
        )
    return (
        f"{tool_name} isn't available to someone from another organisation, who is asking "
        "here. Nothing was done. Tell them someone in the organisation that runs this agent "
        "can do it."
    )
