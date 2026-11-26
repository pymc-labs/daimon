"""A workspace's shared MCP connections, served on Daimon's MCP server.

Each approved tool of a connection appears as `<connection>_<tool>` to a caller who has the
`shared_connections` feature on and holds the tool's scope. Safety (`IdentityMiddleware`) runs
first: the agent's tool rules refuse a name, a write is carded for an agent's credential, an
outside client is told to ask its user first, and every call leaves a security audit row.
The description and schema shown are the ones an admin approved, never the upstream's live text.
What a caller sees is read once per request; a Daimon tool's name is never looked up here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.data_access import data_audience_for
from daimon.adapters.mcp.middleware.capability_gate import caller_places
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core import shared_connections as shared
from daimon.core.data_audience import DataAudience
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.security_audit import record_denial, record_policy_decision
from daimon.data.shared_scopes import holds_scope
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
_SEEN = "shared_connections.visible"
"""Request state: what this request's caller sees (`SharedConnectionProvider._visible`)."""


def register_scopes(runtime: McpRuntime) -> None:
    """At startup: scoped shared tools follow the data store's grants, while the workspace
    has data sources on."""
    settings = runtime.settings

    async def check(caller: shared.SharedCaller, scope: str) -> bool:
        audience = None if caller.audience is None else await caller.audience()
        if audience is None:
            return False
        place = FeaturePlace(caller.tenant_id, caller.platform or "web")
        async with runtime.session_factory() as db:
            if not (await feature_in(db, settings, place, "data_sources")).on:
                return False
        return await holds_scope(settings, caller.tenant_id, audience, scope)

    shared.register_scope_check(check)


def person_audience(account_id: uuid.UUID) -> DataAudience:
    """A key's or the hub's answer goes to its person alone."""
    return DataAudience(
        people=frozenset({account_id}),
        guests=frozenset(),
        public_only=False,
        list_rows=False,
        redact=False,
    )


def caller_of(runtime: McpRuntime, auth: AuthIdentity) -> shared.SharedCaller:
    """`auth` as a shared caller: in chat its audience is the data reads' (the asker and the
    room's viewers), for a key its person."""
    account = auth.requester_account_id or auth.account_id
    found: list[DataAudience | None] = []

    async def audience() -> DataAudience | None:
        if not found:
            if auth.chat_agent_id is None:
                found.append(person_audience(account))
            else:
                try:
                    found.append(await data_audience_for(runtime, auth))
                except ToolError:  # no audience here: no scope is held
                    found.append(None)
        return found[0]

    return shared.SharedCaller(
        tenant_id=auth.tenant_id,
        account_id=account,
        platform=auth.platform,
        chat_session_id=auth.chat_session_id,
        audience=audience,
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


def tool_of(
    runtime: McpRuntime, served: shared.SharedTool, *, outside: bool = False
) -> SharedConnectionTool:
    """The tool as it is shown: the approved words, flagged for clients by what it does."""
    tool = served.tool
    made = SharedConnectionTool(
        name=served.name,
        description=shared.shown_description(served, outside=outside),
        parameters=tool.input_schema or {"type": "object"},
        tags={shared.SERVED_TAG},
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
        seen = await ctx.get_state(_SEEN)
        if isinstance(seen, list):
            return cast("list[shared.SharedTool]", seen)
        auth = await ctx.get_state("auth")
        if not isinstance(auth, AuthIdentity):
            return []
        runtime = self._runtime
        async with runtime.session_factory() as session:
            found = await shared.served_tools(session, caller_of(runtime, auth))
        # Nothing stored, nothing more read: the feature is on by default.
        if found and not await shared.shared_connections_on(
            runtime.session_factory, runtime.settings, await caller_places(runtime, auth, None)
        ):
            found = []
        await ctx.set_state(_SEEN, found, serializable=False)
        return found

    async def _list_tools(self) -> Sequence[Tool]:
        found = await self._visible()
        auth = await get_context().get_state("auth") if found else None
        # An outside client has no card: a write's description tells it to ask its user.
        outside = isinstance(auth, AuthIdentity) and auth.chat_agent_id is None
        return [tool_of(self._runtime, f, outside=outside) for f in found]

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        if name in shared.DAIMON_TOOL_NAMES or not shared.is_served_name(name):
            return None
        found = next((s for s in await self._visible() if s.name == name), None)
        return None if found is None else tool_of(self._runtime, found)


def refuse(reason: str, text: str) -> ToolError:
    record_denial(f"shared_{reason}")
    log.info("shared_connections.refused", reason=reason)
    return ToolError(text)
