"""The hub's view of a workspace's shared MCP connections, for an outside client.

A hub caller is one platform user across several workspaces, so both tools name the
workspace. Each call first passes the checks every hub tool runs (`_auth_for`, `_admit`): the
workspace is live, the account exists, and the access policy admits the caller now. The
feature, the tool's scope and the safety rules are the ones `/mcp` applies; a hub client has
no card to answer, so a tool that changes things says to ask its user first. Every call
leaves a security audit row, the hub having no identity middleware of its own to write it.
"""

from __future__ import annotations

from typing import Any, Literal

from daimon.adapters.mcp.hub.identity import (
    HubIdentity,
    _hub_auth,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.middleware.host_call_gate import schema_problem
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.shared_connections import OPERATION, person_audience
from daimon.adapters.mcp.tools._ctx import _admit  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.hub import _auth_for  # pyright: ignore[reportPrivateUsage]
from daimon.core import shared_connections as shared
from daimon.core.data_audience import DataAudience
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.hub_identity import HubTenant
from daimon.core.stores.security_audit import SecurityAuditEntry, append_event
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, JsonValue

from mcp.types import TextContent

_UNKNOWN = "Not found. Call list_daimons for the workspace_id values you can reach."
_NAME_MAX = 128
_OFF = "Shared connections are turned off there."
_REFUSED = "The tool rules here refuse this tool. Ask an admin. Nothing was done."


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
    async def _served(
        hub: HubIdentity, tenant: HubTenant, tool_name: str
    ) -> list[shared.SharedTool]:
        auth = await _auth_for(runtime, hub, tenant, None)
        await _admit(
            auth,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            billing_config=None,
            markup=runtime.settings.billing.markup,
            tool_name=tool_name,
        )
        place = FeaturePlace(tenant.tenant_id, hub.platform)
        if not await shared.shared_connections_on(
            runtime.session_factory, runtime.settings, [place]
        ):
            raise ToolError(_OFF)

        async def audience() -> DataAudience:  # the hub answers its person alone
            return person_audience(tenant.account_id)

        caller = shared.SharedCaller(
            tenant.tenant_id, tenant.account_id, hub.platform, audience=audience
        )
        async with runtime.session_factory() as session:
            return await shared.served_tools(session, caller)

    async def _audit(
        hub: HubIdentity,
        tenant: HubTenant,
        name: str,
        outcome: Literal["allowed", "denied", "error"],
        reason: str,
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
                    tool_name=name[:_NAME_MAX],
                    operation=OPERATION,
                    outcome=outcome,
                    reason=reason,
                ).model_dump(),
            )

    @mcp.tool
    async def list_shared_tools(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workspace_id: str
    ) -> list[SharedToolInfo]:
        """List the tools a workspace has shared from its connected MCP servers.

        ``workspace_id`` is the ``workspace_id`` of a Daimon from list_daimons. Pass a
        tool's ``name`` to call_shared_tool. A tool with ``writes`` true changes things:
        ask your user and get their yes before you call it.
        """
        hub = await _hub_auth(ctx)
        found = await _served(hub, _tenant(hub, workspace_id), "list_shared_tools")
        return [
            SharedToolInfo(
                name=t.name,
                description=shared.shown_description(t, outside=True),
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
        list_shared_tools. Before a tool with ``writes`` true, ask your user first."""
        hub = await _hub_auth(ctx)
        tenant = _tenant(hub, workspace_id)
        served = await _served(hub, tenant, "call_shared_tool")
        found = next((t for t in served if t.name == name), None)
        if found is None:
            await _audit(hub, tenant, name, "denied", "not_served")
            raise ToolError("No shared tool by that name.")
        args = arguments or {}
        if (problem := schema_problem(found.tool.input_schema, args)) is not None:
            raise ToolError(problem)
        verdict = shared.decide_shared_call(
            runtime.settings.tool_safety,
            found.name,
            args,
            writes=found.tool.writes,
            attended=False,
            agent_rule=None,
            outside=True,
        )
        if verdict.outcome != "allow":
            await _audit(hub, tenant, name, "denied", f"verdict_{verdict.reason}")
            raise ToolError(_REFUSED)
        if runtime.fernet is None:
            raise ToolError("Shared connections aren't set up here.")
        try:
            result = await shared.call_upstream(runtime.fernet, found, dict[str, JsonValue](args))
        except shared.SharedConnectionError as err:
            await _audit(hub, tenant, name, "error", "upstream_failed")
            raise ToolError(str(err)) from err
        await _audit(hub, tenant, name, "allowed", "policy_allow")
        if result.is_error:
            raise ToolError(result.text)
        return ToolResult(content=[TextContent(type="text", text=result.text)])
