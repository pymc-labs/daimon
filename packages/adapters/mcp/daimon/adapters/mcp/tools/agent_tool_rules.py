"""Admin tools for one agent's MCP tool rules, third-party and Daimon's own."""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, authorize
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.security_audit import record_authz_denial, record_policy_decision
from daimon.core.stores import agent_tool_rules as store
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_PERSON_ONLY = (
    "Tool rules can only be changed by a person, from the CLI or the hub, "
    "not by an agent in a conversation."
)


def _require_person(auth: AuthIdentity) -> None:
    if auth.agent_id is not None or auth.chat_agent_id is not None:
        raise ToolError(_PERSON_ONLY)


def _require_rule_access(auth: AuthIdentity, channel_id: str | None) -> None:
    action = (
        Action.SET_AGENT_TOOL_RULE if channel_id is None else Action.SET_CHANNEL_AGENT_TOOL_RULE
    )
    decision = authorize(
        TenantAccessPolicy(),
        subject=mcp_subject(auth, is_admin=auth.is_admin),
        action=action,
        place=Place(channel_id=channel_id),
    )
    if not decision:
        record_authz_denial(action, decision.reason)
        raise ToolError(f"Only {admin_noun(auth.platform)} can manage tool rules.")
    record_policy_decision(action, "allow")


async def _channel(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str | None,
    *,
    lenient: bool = False,
) -> str | None:
    if channel_id is None:
        return None
    return (await resolve_channel(runtime, auth, channel_id, lenient=lenient)).channel_id


async def _agent(runtime: McpRuntime, auth: AuthIdentity, name: str) -> BetaManagedAgentsAgent:
    agent = await find_agent_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if agent is None:
        raise ToolError(f"No agent named {name!r} here.")
    return agent


def _server_url(agent: BetaManagedAgentsAgent, tool_key: str) -> str:
    server, sep, tool = tool_key.partition("/")
    if not sep or not server or not tool or "/" in tool:
        raise ToolError("tool_key must be exactly server/tool")
    for entry in agent.mcp_servers:
        if entry.name == server:
            return entry.url
    raise ToolError(f"Agent has no connection named {server!r}.")


def _active(runtime: McpRuntime) -> bool:
    return runtime.settings.tool_safety.enabled and runtime.settings.tool_safety.agent_rules


async def _list(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    channel_id: str | None = None,
) -> dict[str, object]:
    require_scope(auth, "tenant:read")
    channel = await _channel(runtime, auth, channel_id)
    _require_rule_access(auth, channel)
    agent = await _agent(runtime, auth, agent_name)
    async with runtime.session_factory() as session:
        rows = await store.list_rules(
            session, tenant_id=auth.tenant_id, agent_id=agent.id, channel_id=channel
        )
    return {"rules": rows, "active": _active(runtime)}


async def _set(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    tool_key: str,
    rule: store.Rule,
    scope: store.Scope = "chat",
    channel_id: str | None = None,
) -> dict[str, object]:
    _require_person(auth)
    require_scope(auth, "channels:write")
    channel = await _channel(runtime, auth, channel_id)
    _require_rule_access(auth, channel)
    agent = await _agent(runtime, auth, agent_name)
    url = _server_url(agent, tool_key)
    async with runtime.session_factory.begin() as session:
        row = await store.set_rule(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent.id,
            agent_name=agent.name,
            tool_key=tool_key,
            channel_id=channel,
            server_url=url,
            rule=rule,
            scope=scope,
            set_by=str(auth.account_id),
            actor=mcp_subject(auth, is_admin=auth.is_admin),
        )
    return {"rule": row, "active": _active(runtime)}


async def _clear(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    tool_key: str,
    channel_id: str | None = None,
) -> dict[str, object]:
    _require_person(auth)
    require_scope(auth, "channels:write")
    channel = await _channel(runtime, auth, channel_id, lenient=True)
    _require_rule_access(auth, channel)
    agent = await _agent(runtime, auth, agent_name)
    async with runtime.session_factory.begin() as session:
        existing = await store.get_rule(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent.id,
            tool_key=tool_key,
            channel_id=channel,
        )
        url = existing.server_url if existing is not None else _server_url(agent, tool_key)
        row = await store.clear_rule(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent.id,
            agent_name=agent.name,
            tool_key=tool_key,
            channel_id=channel,
            server_url=url,
            set_by=str(auth.account_id),
            actor=mcp_subject(auth, is_admin=auth.is_admin),
        )
    return {"rule": row, "active": _active(runtime)}


def register_agent_tool_rule_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def list_agent_tool_rules(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_name: str, channel_id: str | None = None
    ) -> dict[str, object]:
        """List an agent's tool rules, optionally for one channel."""
        return await _list(runtime, await _auth(ctx), agent_name, channel_id)

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def set_agent_tool_rule(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        tool_key: str,
        rule: store.Rule,
        scope: store.Scope = "chat",
        channel_id: str | None = None,
    ) -> dict[str, object]:
        """Set one agent's tool rule."""
        return await _set(runtime, await _auth(ctx), agent_name, tool_key, rule, scope, channel_id)

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def clear_agent_tool_rule(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        tool_key: str,
        channel_id: str | None = None,
    ) -> dict[str, object]:
        """Clear one agent's tool rule."""
        return await _clear(runtime, await _auth(ctx), agent_name, tool_key, channel_id)
