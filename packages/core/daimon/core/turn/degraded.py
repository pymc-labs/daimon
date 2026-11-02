"""Words for a turn that came to nothing because its MCP servers failed.

Pure. The driver uses `degraded_failure_message` when the turn produced
nothing at all, so the failure names the server rather than a bare
"upstream". A turn that answered without a server is not the person's
business here: the agent hears of it (`agent_notices.mcp_failure_notices`).
"""

from __future__ import annotations

from collections.abc import Sequence

from daimon.core.turn.state import McpServerFailure


def degraded_failure_message(failures: Sequence[McpServerFailure]) -> str:
    """The error text for a reply that came to nothing after connection failures."""
    names = ", ".join(f"'{f.server_name}'" for f in failures)
    detail = failures[-1].message
    return f"Connection {names} failed, so there is no reply: {detail}"
