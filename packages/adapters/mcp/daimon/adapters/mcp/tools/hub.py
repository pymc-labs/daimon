"""Tool surface for the hub mounts: every daimon a logged-in person can reach.

A hub caller is one platform user across several tenants, so no tool here
takes a bare agent name: names are unique only within a tenant and two guilds
can both have a ``helper``. ``list_daimons`` hands out the derived per-agent
UUID as ``id`` and every other tool takes it back as ``daimon_id``. Resolution
walks only the caller's own tenants, so an id from anywhere else is
indistinguishable from a nonexistent one.

Once a daimon is resolved the tool builds an ordinary ``AuthIdentity`` for the
caller's account in that tenant and hands off to the agent-chat implementation
functions. That is what makes a hub turn run with the person's channel
visibility and bill the right tenant, exactly as a per-agent token does.
"""

from __future__ import annotations

import uuid
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hub.identity import (
    HubIdentity,
    _hub_auth,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _admit  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pagination import Page
from daimon.adapters.mcp.tools.agent_chat import (
    AgentDescription,
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
    _ask_tool_result,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _describe_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _get_session_impl,  # pyright: ignore[reportPrivateUsage]
    _list_events_impl,  # pyright: ignore[reportPrivateUsage]
    _list_sessions_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.sessions import SessionEventOut, SessionInfo
from daimon.core.billing import BillingConfig
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel

_NOT_FOUND = "daimon not found"


class DaimonSummary(BaseModel):
    model_config = {"frozen": True}

    id: str
    name: str
    platform: str
    workspace_id: str
    workspace: str
    role_summary: str
    skill_names: list[str]


def _summary(tenant: HubTenant, hub: HubIdentity, agent: BetaManagedAgentsAgent) -> DaimonSummary:
    return DaimonSummary(
        id=str(derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id=str(agent.id))),
        name=agent.name,
        platform=hub.platform,
        workspace_id=tenant.workspace_id,
        workspace=tenant.workspace_name,
        role_summary=(agent.system or "")[:200],
        skill_names=[sk.skill_id for sk in agent.skills],
    )


async def _list_daimons_impl(runtime: McpRuntime, hub: HubIdentity) -> list[DaimonSummary]:
    out: list[DaimonSummary] = []
    for tenant in hub.tenants:
        for agent in await list_agents_by_tenant(runtime.client, tenant_id=tenant.tenant_id):
            out.append(_summary(tenant, hub, agent))
    out.sort(key=lambda d: (d.workspace.lower(), d.name.lower()))
    return out


async def _resolve_daimon(
    runtime: McpRuntime, hub: HubIdentity, daimon_id: str
) -> tuple[HubTenant, BetaManagedAgentsAgent]:
    try:
        wanted = uuid.UUID(daimon_id)
    except ValueError as e:
        raise ToolError(_NOT_FOUND) from e
    for tenant in hub.tenants:
        for agent in await list_agents_by_tenant(runtime.client, tenant_id=tenant.tenant_id):
            if derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id=str(agent.id)) == wanted:
                return tenant, agent
    raise ToolError(_NOT_FOUND)


async def _auth_for(
    runtime: McpRuntime, hub: HubIdentity, tenant: HubTenant, agent: BetaManagedAgentsAgent
) -> AuthIdentity:
    """Identity for the caller's account in ``tenant``. Never admin: the hub has no admin tools."""
    async with runtime.session_factory() as session:
        row = await get_account_with_tenant(session, account_id=tenant.account_id)
    if row is None:
        raise ToolError(_NOT_FOUND)
    return AuthIdentity(
        account_id=tenant.account_id,
        tenant_id=tenant.tenant_id,
        role=Role.USER if row.role is not Role.ADMIN else Role.ADMIN,
        platform=hub.platform,
        external_id=tenant.workspace_id,
        agent_id=derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id=str(agent.id)),
        platform_user_id=hub.platform_user_id,
        is_admin=False,
    )


async def _identity(
    runtime: McpRuntime, ctx: Context, daimon_id: str
) -> tuple[HubTenant, AuthIdentity]:
    hub = await _hub_auth(ctx)
    tenant, agent = await _resolve_daimon(runtime, hub, daimon_id)
    return tenant, await _auth_for(runtime, hub, tenant, agent)


def register_hub_tools(
    mcp: FastMCP, runtime: McpRuntime, *, billing_config: BillingConfig | None
) -> None:
    async def _admitted(ctx: Context, daimon_id: str, tool_name: str) -> AuthIdentity:
        _, auth = await _identity(runtime, ctx, daimon_id)
        return await _admit(
            auth,
            sessionmaker=runtime.session_factory,
            billing_config=billing_config,
            tool_name=tool_name,
        )

    @mcp.tool
    async def list_daimons(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> list[DaimonSummary]:
        """List every daimon you can reach on this platform, across all your workspaces.

        Call this first. ``id`` is what every other tool takes as ``daimon_id``;
        ``workspace`` tells you which Slack workspace or Discord server the daimon
        lives in. Two daimons may share a ``name`` in different workspaces.
        """
        return await _list_daimons_impl(runtime, await _hub_auth(ctx))

    @mcp.tool
    async def describe_daimon(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str
    ) -> AgentDescription:
        """Describe one daimon: role, skills, repo, environment, platform and workspace."""
        tenant, auth = await _identity(runtime, ctx, daimon_id)
        base = await _describe_agent_impl(runtime, auth)
        return base.model_copy(update={"workspace": tenant.workspace_name})

    @mcp.tool
    async def ask(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str, message: str, handle: str | None = None
    ) -> ToolResult:
        """Ask a daimon one question and wait up to about two minutes for its answer.

        Runs as you, in that daimon's workspace: it reads only what you can see
        and spends that workspace's credit. Pass ``handle`` from a previous
        result to continue the same conversation. On timeout the error carries
        the handle; resume with it rather than asking again.
        """
        auth = await _admitted(ctx, daimon_id, "ask")
        return _ask_tool_result(await _ask_impl(runtime, auth, message, handle=handle))

    @mcp.tool
    async def start_turn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str, message: str
    ) -> dict[str, str]:
        """Start a turn without waiting.

        Returns ``{"handle": ...}`` for polling with ``get_session``.
        """
        auth = await _admitted(ctx, daimon_id, "start_turn")
        return await _start_turn_impl(runtime, auth, message)

    @mcp.tool
    async def continue_turn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str, handle: str, message: str
    ) -> dict[str, str]:
        """Send a follow-up on an existing session without waiting."""
        auth = await _admitted(ctx, daimon_id, "continue_turn")
        return await _continue_turn_impl(runtime, auth, handle, message)

    @mcp.tool
    async def get_session(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str, handle: str
    ) -> SessionInfo:
        """Status of one session. Poll until ``idle`` before reading ``list_events``."""
        _, auth = await _identity(runtime, ctx, daimon_id)
        return await _get_session_impl(runtime, auth, handle)

    @mcp.tool
    async def list_events(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        daimon_id: str,
        handle: str,
        page: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] | None = None,
    ) -> Page[SessionEventOut]:
        """A session's transcript. The daimon's reply is in ``agent.message`` events."""
        _, auth = await _identity(runtime, ctx, daimon_id)
        return await _list_events_impl(runtime, auth, handle, page, limit, order)

    @mcp.tool
    async def list_my_sessions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str
    ) -> list[SessionInfo]:
        """Sessions you have with this daimon, for resuming with ``handle``."""
        _, auth = await _identity(runtime, ctx, daimon_id)
        return await _list_sessions_impl(runtime, auth)
