"""The hub's view of a workspace's shared MCP connections, for an outside client.

A hub caller is one platform user across several workspaces, so both tools name the
workspace. The feature, the tool's scope and the safety rules are the ones `/mcp` applies; a
hub client has no card to answer, so a read runs and a write is refused. Every call leaves a
security audit row, the hub having no identity middleware of its own to write it.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.mcp.hub.identity import (
    HubIdentity,
    _hub_auth,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.shared_connections import OPERATION
from daimon.core import shared_connections as shared
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.hub_identity import HubTenant
from daimon.core.stores.security_audit import SecurityAuditEntry, append_event
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, JsonValue

from mcp.types import TextContent

_UNKNOWN = "No such workspace. Call list_daimons to see the ones you can reach."
_OFF = "Shared connections are turned off in that workspace."
_NO_WRITES = (
    "This tool changes things and needs a person's approval, which this client can't ask "
    "for. Nothing was done. Use it from a chat with a Daimon."
)


class SharedToolInfo(BaseModel):
    name: str
    description: str
    writes: bool
    input_schema: dict[str, Any]


def _tenant(hub: HubIdentity, workspace_id: str) -> HubTenant:
    found = next((t for t in hub.tenants if t.workspace_id == workspace_id), None)
    if found is None:
        raise ToolError(_UNKNOWN)
    return found


def register_hub_shared_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    async def _served(hub: HubIdentity, tenant: HubTenant) -> list[shared.SharedTool]:
        place = FeaturePlace(tenant.tenant_id, hub.platform)
        if not await shared.shared_connections_on(
            runtime.session_factory, runtime.settings, [place]
        ):
            raise ToolError(_OFF)
        caller = shared.SharedCaller(tenant.tenant_id, tenant.account_id, hub.platform)
        async with runtime.session_factory() as session:
            return await shared.served_tools(session, caller)

    async def _audit(
        hub: HubIdentity, tenant: HubTenant, name: str, outcome: str, reason: str
    ) -> None:
        async with runtime.session_factory.begin() as session:
            await append_event(
                session,
                **SecurityAuditEntry(
                    tenant_id=tenant.tenant_id,
                    account_id=tenant.account_id,
                    agent_id=None,
                    platform=hub.platform,
                    platform_user_id=hub.platform_user_id,
                    tool_name=name,
                    operation=OPERATION,
                    outcome="allowed" if outcome == "allowed" else "denied",
                    reason=reason,
                ).model_dump(),
            )

    @mcp.tool
    async def list_shared_tools(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workspace_id: str
    ) -> list[SharedToolInfo]:
        """List the tools a workspace has shared from its connected MCP servers.

        ``workspace_id`` is the ``workspace_id`` of a Daimon from list_daimons. Pass a
        tool's ``name`` to call_shared_tool. A tool with ``writes`` true changes things
        and can't be called from here.
        """
        hub = await _hub_auth(ctx)
        found = await _served(hub, _tenant(hub, workspace_id))
        return [
            SharedToolInfo(
                name=t.name,
                description=t.tool.description,
                writes=t.tool.writes,
                input_schema=t.tool.input_schema,
            )
            for t in found
        ]

    @mcp.tool
    async def call_shared_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workspace_id: str, name: str, arguments: dict[str, Any] | None = None
    ) -> ToolResult:
        """Call one shared tool of a workspace's connected MCP servers, as listed by
        list_shared_tools. Only tools that don't change anything run from here."""
        hub = await _hub_auth(ctx)
        tenant = _tenant(hub, workspace_id)
        found = next((t for t in await _served(hub, tenant) if t.name == name), None)
        if found is None:
            await _audit(hub, tenant, name, "denied", "not_served")
            raise ToolError(f"No shared tool named {name}.")
        args = arguments or {}
        verdict = shared.decide_shared_call(
            runtime.settings.tool_safety,
            found.name,
            args,
            writes=found.tool.writes,
            attended=False,
            agent_rule=None,
        )
        if verdict.outcome != "allow":
            await _audit(hub, tenant, name, "denied", f"verdict_{verdict.reason}")
            raise ToolError(_NO_WRITES)
        if runtime.fernet is None:
            raise ToolError("Shared connections aren't set up here.")
        try:
            result = await shared.call_upstream(runtime.fernet, found, dict[str, JsonValue](args))
        except shared.SharedConnectionError as err:
            await _audit(hub, tenant, name, "denied", "upstream_failed")
            raise ToolError(str(err)) from err
        await _audit(hub, tenant, name, "allowed", "policy_allow")
        if result.is_error:
            raise ToolError(result.text)
        return ToolResult(content=[TextContent(type="text", text=result.text)])
