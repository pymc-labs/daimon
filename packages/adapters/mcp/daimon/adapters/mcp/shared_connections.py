"""A workspace's shared MCP connections, served on Daimon's MCP server.

Each approved tool of a connection appears as `<connection>_<tool>` to a caller who has the
`shared_connections` feature on and holds the tool's scope. Safety (`IdentityMiddleware`) runs
first: the agent's tool rules refuse a name, a write is carded for a chat credential and
refused for a client with no way to answer one, and every call leaves a security audit row.
The description and schema shown are the ones an admin approved, never the upstream's live text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.capability_gate import caller_places
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core import shared_connections as shared
from daimon.core.security_audit import record_denial, record_policy_decision
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec
from pydantic import JsonValue, PrivateAttr

from mcp.types import TextContent, ToolAnnotations

log = structlog.get_logger(__name__)

#: The operation the security audit row names for a call of a shared tool.
OPERATION = "shared_connection.call"


def caller_of(auth: AuthIdentity) -> shared.SharedCaller:
    return shared.SharedCaller(
        tenant_id=auth.tenant_id,
        account_id=auth.requester_account_id or auth.account_id,
        platform=auth.platform,
        chat_session_id=auth.chat_session_id,
    )


class SharedConnectionTool(Tool):
    """One approved upstream tool, run with the connection's credential."""

    _served: shared.SharedTool = PrivateAttr()
    _runtime: McpRuntime = PrivateAttr()

    @property
    def served(self) -> shared.SharedTool:
        return self._served

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self._runtime.fernet is None:
            raise ToolError("Shared connections aren't set up here.")
        record_policy_decision(OPERATION, "allow")
        try:
            result = await shared.call_upstream(
                self._runtime.fernet, self._served, dict[str, JsonValue](arguments)
            )
        except shared.SharedConnectionError as err:
            raise ToolError(str(err)) from err
        if result.is_error:
            raise ToolError(result.text)
        return ToolResult(content=[TextContent(type="text", text=result.text)])


def tool_of(runtime: McpRuntime, served: shared.SharedTool) -> SharedConnectionTool:
    """The tool as it is shown: the approved words, flagged for clients by what it does."""
    tool = served.tool
    made = SharedConnectionTool(
        name=served.name,
        description=(tool.description or f"{served.connection.name}: {tool.name}"),
        parameters=tool.input_schema or {"type": "object"},
        annotations=ToolAnnotations(
            readOnlyHint=not tool.writes, destructiveHint=tool.writes, openWorldHint=True
        ),
    )
    made._served = served  # pyright: ignore[reportPrivateUsage]
    made._runtime = runtime  # pyright: ignore[reportPrivateUsage]
    return made


class SharedConnectionProvider(Provider):
    """The tools the caller of this request may use; nothing for a request with no identity."""

    def __init__(self, runtime: McpRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    async def _visible(self) -> list[shared.SharedTool]:
        try:
            ctx = get_context()
        except RuntimeError:
            return []
        auth = await ctx.get_state("auth")
        if not isinstance(auth, AuthIdentity):
            return []
        runtime = self._runtime
        places = await caller_places(runtime, auth, None)
        if not await shared.shared_connections_on(
            runtime.session_factory, runtime.settings, places
        ):
            return []
        async with runtime.session_factory() as session:
            return await shared.served_tools(session, caller_of(auth))

    async def _list_tools(self) -> Sequence[Tool]:
        return [tool_of(self._runtime, found) for found in await self._visible()]

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        if not shared.is_served_name(name):
            return None
        found = next((s for s in await self._visible() if s.name == name), None)
        return None if found is None else tool_of(self._runtime, found)


def refuse(reason: str, text: str) -> ToolError:
    record_denial(f"shared_{reason}")
    log.info("shared_connections.refused", reason=reason)
    return ToolError(text)
