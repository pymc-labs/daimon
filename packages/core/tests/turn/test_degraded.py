"""`daimon.core.turn.degraded`: the words for a turn missing an MCP server."""

from __future__ import annotations

from daimon.core.turn.degraded import degraded_failure_message
from daimon.core.turn.state import McpServerFailure

_AUTH = McpServerFailure(
    server_name="notion",
    error_type="mcp_authentication_failed_error",
    message="MCP server 'notion' initialize failed: access forbidden",
    retry_status="exhausted",
)
_CONN = McpServerFailure(
    server_name="linear",
    error_type="mcp_connection_failed_error",
    message="timeout",
    retry_status="exhausted",
)


def test_degraded_failure_message_names_servers_and_carries_ma_detail() -> None:
    message = degraded_failure_message((_AUTH,))
    assert "'notion'" in message, "the failure must name the server"
    assert "access forbidden" in message, "MA's own detail is the actionable part"
