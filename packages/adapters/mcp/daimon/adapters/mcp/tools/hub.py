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

Session handles are scoped one step tighter than on the per-agent surface.
There, the token *is* the agent, so "the agent's sessions" and "the caller's
sessions" coincide. Here every member of a workspace shares one daimon, so a
handle is only usable by the account that created the session: the
``daimon_account`` metadata ``create_session`` stamps on every session is
compared to the caller's account before any read or continuation, and
``list_my_sessions`` filters on it. Channel threads driven by the chat
adapters carry the thread starter's account, so they are invisible to
everyone else through the hub.
"""

from __future__ import annotations

import uuid
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSession
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
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _verify_agent_owns_session,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.sessions import SessionEventOut, SessionInfo
from daimon.core.billing import BillingConfig
from daimon.core.defaults.ma_index import list_agents_by_tenants
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel

_NOT_FOUND = "daimon not found"
_SESSION_NOT_FOUND = "session not found"


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


async def _agents_by_tenant(
    runtime: McpRuntime, hub: HubIdentity
) -> list[tuple[HubTenant, list[BetaManagedAgentsAgent]]]:
    by_id = await list_agents_by_tenants(
        runtime.client, tenant_ids=[t.tenant_id for t in hub.tenants]
    )
    return [(tenant, by_id[tenant.tenant_id]) for tenant in hub.tenants]


async def _list_daimons_impl(runtime: McpRuntime, hub: HubIdentity) -> list[DaimonSummary]:
    out: list[DaimonSummary] = []
    for tenant, agents in await _agents_by_tenant(runtime, hub):
        out.extend(_summary(tenant, hub, agent) for agent in agents)
    out.sort(key=lambda d: (d.workspace.lower(), d.name.lower()))
    return out


async def _resolve_daimon(
    runtime: McpRuntime, hub: HubIdentity, daimon_id: str
) -> tuple[HubTenant, BetaManagedAgentsAgent]:
    try:
        wanted = uuid.UUID(daimon_id)
    except ValueError as e:
        raise ToolError(_NOT_FOUND) from e
    for tenant, agents in await _agents_by_tenant(runtime, hub):
        for agent in agents:
            if derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id=str(agent.id)) == wanted:
                return tenant, agent
    raise ToolError(_NOT_FOUND)


async def _auth_for(
    runtime: McpRuntime, hub: HubIdentity, tenant: HubTenant, agent: BetaManagedAgentsAgent
) -> AuthIdentity:
    """Identity for the caller's account in ``tenant``.

    ``role`` and ``is_admin`` are both pinned to the non-admin value rather
    than copied from the account row: the hub registers no admin tools, and
    every admin gate in the codebase expects the pair to agree.

    The tenant's readiness is re-checked here rather than trusted from the
    login-time claims, so a workspace that uninstalls daimon or is archived
    stops being reachable on the next call instead of when the token expires.
    """
    async with runtime.session_factory() as session:
        row = await get_account_with_tenant(session, account_id=tenant.account_id)
        live = await get_tenant(session, tenant.tenant_id)
    if (
        row is None
        or live is None
        or live.archived_at is not None
        or live.provision_status != "ready"
    ):
        raise ToolError(_NOT_FOUND)
    return AuthIdentity(
        account_id=tenant.account_id,
        tenant_id=tenant.tenant_id,
        role=Role.USER,
        platform=hub.platform,
        external_id=tenant.workspace_id,
        agent_id=derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id=str(agent.id)),
        platform_user_id=hub.platform_user_id,
        is_admin=False,
    )


async def _identity(
    runtime: McpRuntime, ctx: Context, daimon_id: str
) -> tuple[HubTenant, BetaManagedAgentsAgent, AuthIdentity]:
    hub = await _hub_auth(ctx)
    tenant, agent = await _resolve_daimon(runtime, hub, daimon_id)
    return tenant, agent, await _auth_for(runtime, hub, tenant, agent)


def _owned_by(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    return session.metadata.get(MA_METADATA_KEY_ACCOUNT) == str(auth.account_id)


async def _verify_account_owns_session(
    runtime: McpRuntime, auth: AuthIdentity, handle: str
) -> None:
    """Reject a handle unless the caller's account created the session.

    Same message as an unknown handle, so a session's existence is not leaked
    to other members of the workspace.
    """
    session = await _verify_agent_owns_session(runtime, auth, handle)
    if not _owned_by(session, auth):
        raise ToolError(_SESSION_NOT_FOUND)


async def _list_my_sessions_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent: BetaManagedAgentsAgent
) -> list[SessionInfo]:
    out: list[SessionInfo] = []
    async for session in runtime.client.beta.sessions.list(agent_id=str(agent.id)):
        if _owned_by(session, auth):
            out.append(SessionInfo.from_ma(session))
    return out


def register_hub_tools(
    mcp: FastMCP, runtime: McpRuntime, *, billing_config: BillingConfig | None
) -> None:
    async def _admitted(ctx: Context, daimon_id: str, tool_name: str) -> AuthIdentity:
        _, _, auth = await _identity(runtime, ctx, daimon_id)
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
        tenant, _, auth = await _identity(runtime, ctx, daimon_id)
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
        if handle is not None:
            await _verify_account_owns_session(runtime, auth, handle)
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
        await _verify_account_owns_session(runtime, auth, handle)
        return await _continue_turn_impl(runtime, auth, handle, message)

    @mcp.tool
    async def get_session(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str, handle: str
    ) -> SessionInfo:
        """Status of one session. Poll until ``idle`` before reading ``list_events``."""
        _, _, auth = await _identity(runtime, ctx, daimon_id)
        await _verify_account_owns_session(runtime, auth, handle)
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
        _, _, auth = await _identity(runtime, ctx, daimon_id)
        await _verify_account_owns_session(runtime, auth, handle)
        return await _list_events_impl(runtime, auth, handle, page, limit, order)

    @mcp.tool
    async def list_my_sessions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, daimon_id: str
    ) -> list[SessionInfo]:
        """Sessions you started with this daimon, for resuming with ``handle``.

        Other people's conversations with the same daimon are not listed and
        their handles are not accepted.
        """
        _, agent, auth = await _identity(runtime, ctx, daimon_id)
        return await _list_my_sessions_impl(runtime, auth, agent)
