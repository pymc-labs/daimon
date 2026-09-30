"""Per-target runtime gate: is the named agent currently reachable in this tenant?

Blast-radius rule for whoever adds the next tool: **blast radius of one
agent -> open; blast radius of the whole tenant -> admin.** Creating, forking,
or configuring an agent nobody has scoped only ever affects that one agent, so
it stays open to any member. The moment an agent is a channel or workspace
default, changing the fields that shape what it says and does reaches every
user who talks to it — that requires admin.

``description`` is deliberately not in the gated set: it is a label, not
something that changes what the agent does or can reach. ``tools`` IS in the
gated set even though ``mcp_servers`` might look like the more obvious target:
an ``mcp_toolset`` entry in ``tools`` is how an attached MCP server actually
becomes reachable by the model, so gating ``mcp_servers`` while leaving
``tools`` open would let a non-admin route around the gate entirely — it
would be no gate at all.
"""

from __future__ import annotations

from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.agent_reach import load_target_facts, may_bind_as_channel_default
from daimon.core.channel_admins import ChannelAdminCaller, is_channel_admin
from daimon.core.operation_policy import OperationKind, TargetFacts, decide_operation
from daimon.core.stores.channel_admins import get_channel_admins
from fastmcp.exceptions import ToolError

REACHABILITY_GATED_FIELDS: Final[frozenset[str]] = frozenset(
    {"system", "model", "skills", "mcp_servers", "tools"}
)


def channel_admin_caller(auth: AuthIdentity) -> ChannelAdminCaller:
    """The caller as channel admin grants see them. An agent credential is nobody."""
    return ChannelAdminCaller(
        platform_user_id=auth.platform_user_id if auth.agent_id is None else None,
        role_ids=frozenset(auth.platform_role_ids) if auth.agent_id is None else frozenset(),
        is_server_admin=auth.is_admin,
    )


async def target_facts(
    runtime: McpRuntime,
    auth: AuthIdentity,
    operation: OperationKind,
    *,
    agent_name: str,
    is_daimon_managed: bool,
) -> TargetFacts:
    """Policy facts for `agent_name` in the caller's own tenant, read only when needed."""
    async with runtime.session_factory() as session:
        return await load_target_facts(
            session,
            operation,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            agent_name=agent_name,
            default=runtime.deployment_default,
            caller=channel_admin_caller(auth),
            is_daimon_managed=is_daimon_managed,
        )


async def require_channel_admin(
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str
) -> None:
    """Raise ``ToolError`` unless the caller is a server admin or administers `channel_id`."""
    if auth.is_admin:
        return
    grant = None
    if auth.platform is not None and auth.agent_id is None:
        async with runtime.session_factory() as session:
            grant = await get_channel_admins(
                session, tenant_id=auth.tenant_id, platform=auth.platform, channel_id=channel_id
            )
    if not is_channel_admin(channel_admin_caller(auth), grant=grant):
        raise ToolError(
            "This change needs a workspace or server admin, or an admin of that channel, "
            "and the caller is neither. Tell them who can make it and give them a sentence "
            "that admin can say, preserving the requested action and channel. Do not retry."
        )


async def require_bindable_by_channel_admin(
    runtime: McpRuntime, auth: AuthIdentity, *, agent_name: str, is_daimon_managed: bool
) -> None:
    """Raise ``ToolError`` when a channel admin binds another channel's own agent."""
    if auth.is_admin:
        return
    async with runtime.session_factory() as session:
        allowed = await may_bind_as_channel_default(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            agent_name=agent_name,
            default=runtime.deployment_default,
            caller=channel_admin_caller(auth),
            is_daimon_managed=is_daimon_managed,
        )
    if not allowed:
        raise ToolError(
            f"'{agent_name}' answers in channels this caller does not administer, so only a "
            "workspace or server admin can make it this channel's default. A channel admin may "
            "pick a built-in agent, the workspace default, an agent that answers nowhere yet, "
            "or one that answers only in their channels. Nothing was changed. Do not retry."
        )


async def require_admin_for_reachable_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
) -> None:
    """Raise ``ToolError`` if a non-admin caller is patching a currently-reachable agent.

    Returns immediately for an admin caller without touching the database.
    For a non-admin caller, reads the tenant's config cascade and raises when
    ``agent_name`` currently resolves for any user in this tenant (channel,
    tenant, or deployment tier), unless the caller administers every channel
    the agent answers in. ``tenant_id`` always comes from ``auth.tenant_id`` —
    never from a caller-supplied parameter — so a caller cannot point the read
    at another tenant's config rows. The decision itself is
    `daimon.core.operation_policy.decide_operation`'s.
    """
    if auth.is_admin:
        return
    facts = await target_facts(
        runtime, auth, "agent_spec_edit", agent_name=agent_name, is_daimon_managed=False
    )
    outcome = decide_operation("agent_spec_edit", is_admin=False, target=facts)
    if outcome == "needs_admin":
        raise ToolError(
            f"'{agent_name}' is currently the default agent for this workspace or a "
            "channel, so an admin must change its setup. Tell the caller to ask a workspace "
            f"or server admin to make the requested change to '{agent_name}'; carry the "
            "specific action from the conversation into that handoff. Do not retry. "
            "Creating or forking your own agent is not gated."
        )
