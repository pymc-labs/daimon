"""What went wrong around a turn, told to the agent instead of the person.

A failure that does not stop a turn is never hardcoded text beside the reply:
the agent hears of it here and decides whether, and how, to tell the person.
A notice rides in front of the next user message the agent gets: this turn's
when Daimon knew before sending it, else the next one in the thread (no
backend takes new input mid-turn). Pure: the caller stores and sends.
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.sax.saxutils import escape

from daimon.core.turn.state import McpServerFailure

__all__ = [
    "GITHUB_KEY_RESTART_NOTICE",
    "HANDED_OVER_NOTICE",
    "LOST_WORKSPACE_NOTICE",
    "mcp_failure_notices",
    "render_agent_notices",
]

_REASON = {
    "mcp_connection_failed_error": "could not be reached",
    "mcp_authentication_failed_error": "rejected its credentials",
}

GITHUB_KEY_RESTART_NOTICE = (
    "The GitHub connection was updated, so this conversation restarted in a new workspace. "
    "Earlier messages are still in the chat; unsaved work from before is gone."
)

# Files carry over only when the new agent may read them, so this never promises them.
HANDED_OVER_NOTICE = (
    "Someone here handed this conversation over to you. "
    "You answer with your own keys, connections and memory."
)

LOST_WORKSPACE_NOTICE = (
    "The workspace was lost during your last reply and replaced with a new one. "
    "Working files and running processes from before are gone; say what is missing "
    "before you continue."
)


def _server(name: str) -> str:
    """A server name fit for one line of the block: no markup, no line breaks, short."""
    return escape(" ".join(name.split()).replace("`", "'")[:80])


def mcp_failure_notices(failures: Sequence[McpServerFailure]) -> tuple[str, ...]:
    """One notice per MCP server a reply went without."""
    return tuple(
        f"The `{_server(f.server_name)}` connection {_REASON[f.error_type]} during your last "
        "reply, so its tools were missing. The person can ask you to reconnect or disconnect it."
        for f in failures
    )


def render_agent_notices(notices: Sequence[str]) -> str:
    """The block for the front of a user message, or ``""`` when there is nothing."""
    if not notices:
        return ""
    lines = "\n".join(f"- {notice}" for notice in notices)
    return (
        "<daimon_notices>\n"
        "From Daimon, not the person. Mention one only if it matters to their request, "
        "briefly and in your own words.\n"
        f"{lines}\n"
        "</daimon_notices>"
    )
