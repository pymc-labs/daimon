"""Words for a turn that finished without one of its MCP servers (#79).

Pure. The adapters append `render_degraded_notice` under the reply so the
person learns a named server was unavailable instead of getting a blank
failure; the driver uses `degraded_failure_message` when the turn produced
nothing at all, so the failure names the server rather than a bare
"upstream".
"""

from __future__ import annotations

from collections.abc import Sequence

from daimon.core.turn.state import McpServerFailure

_REASON = {
    "mcp_connection_failed_error": "could not be reached",
    "mcp_authentication_failed_error": "rejected the connection's credentials",
}


def render_degraded_notice(failures: Sequence[McpServerFailure]) -> str | None:
    """One line per failed server, or None when nothing failed."""
    if not failures:
        return None
    lines = [
        f"⚠️ `{f.server_name}` was unavailable this turn: it {_REASON[f.error_type]}. "
        "Ask to reconnect it, or to disconnect it."
        for f in failures
    ]
    return "\n".join(lines)


def degraded_failure_message(failures: Sequence[McpServerFailure]) -> str:
    """The error text for a turn that produced nothing after MCP failures."""
    names = ", ".join(f"'{f.server_name}'" for f in failures)
    detail = failures[-1].message
    return f"MCP server {names} failed and the turn produced no reply: {detail}"
