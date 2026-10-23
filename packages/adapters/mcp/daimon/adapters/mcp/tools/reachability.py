"""Per-target runtime gate: is the named agent currently reachable in this tenant?

An admin may edit a target's setup. A member must administer every channel
named by its agent rule, and every place it answers or runs must stay inside
those channels. An unbound agent is never assumed to belong to the caller.

Every spec field, including ``description``, requires mutation ownership. ``tools`` IS in the
gated set even though ``mcp_servers`` might look like the more obvious target:
an ``mcp_toolset`` entry in ``tools`` is how an attached MCP server actually
becomes reachable by the model, so gating ``mcp_servers`` while leaving
``tools`` open would let a non-admin route around the gate entirely — it
would be no gate at all.
"""

from __future__ import annotations

from typing import Final

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL, agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, authorize
from daimon.core.native_admins import AdminCaller
from daimon.core.operation_policy import OperationKind, TargetFacts
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from fastmcp.exceptions import ToolError

REACHABILITY_GATED_FIELDS: Final[frozenset[str]] = frozenset(
    {"system", "model", "skills", "mcp_servers", "tools", "description"}
)


def admin_caller(auth: AuthIdentity) -> AdminCaller:
    """The caller's native admin status; an agent key has no admin authority."""
    return AdminCaller(
        platform_user_id=auth.platform_user_id if auth.agent_id is None else None,
        is_server_admin=auth.is_platform_admin and auth.agent_id is None,
        role_ids=auth.verified_role_ids,
    )


async def target_facts(
    runtime: McpRuntime,
    auth: AuthIdentity,
    operation: OperationKind,
    *,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    is_daimon_managed: bool,
) -> TargetFacts:
    return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)


async def require_admin(runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str) -> None:
    """Refuse a channel change unless the caller is a native admin."""
    if auth.admin_for(DelegatedAction.CHANNEL_PERMISSIONS) and auth.agent_id is None:
        return
    raise ToolError(
        "This change needs a workspace or server admin. Tell the caller who can make "
        "it and give them a sentence that admin can say, preserving the requested "
        "action and channel. Do not retry."
    )


def _target_names(agent_name: str, agent: BetaManagedAgentsAgent | None) -> tuple[str | None, ...]:
    """The name asked for plus, once resolved, every name the agent itself carries."""
    if agent is None:
        return (agent_name,)
    return (agent_name, *agent_pin_names(agent.name, agent.metadata))


async def require_bindable_as_channel_default(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    agent_name: str,
    agent: BetaManagedAgentsAgent | None,
    is_daimon_managed: bool,
) -> None:
    """Raise ``ToolError`` when the caller cannot bind this agent here."""
    names = tuple(name for name in _target_names(agent_name, agent) if name)
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
        # Nobody, server admins included: the agent would refuse every turn here.
        decision = authorize(
            policy,
            subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.CHANNEL_PERMISSIONS)),
            action=Action.BIND_CHANNEL_DEFAULT,
            agent=AgentRef.of(*names),
            place=Place(channel_id=channel_id),
        )
        if not decision:
            record_authz_denial(Action.BIND_CHANNEL_DEFAULT, decision.reason)
        if decision.reason == "own_agents_only":
            raise ToolError(
                "This channel is kept to its own agents (whose rule runs them here alone), so "
                f"only they can be its default, and '{agent_name}' is not one. Nothing was "
                "changed. Pick one of its own agents, or ask a server admin. Do not retry."
            )
        if decision.reason == "runs_elsewhere":
            raise ToolError(
                f"'{agent_name}'s rule runs it only in other channels, so it would refuse "
                "every turn here and cannot be this channel's default. Nothing was changed. "
                "Pick another agent, or ask an admin to change its rule. Do not retry."
            )
        if auth.admin_for(DelegatedAction.CHANNEL_PERMISSIONS) and auth.agent_id is None:
            return
    raise ToolError(
        "Only a workspace or server admin can make this the channel's default. "
        "Nothing was changed. Do not retry."
    )


async def require_admin_for_reachable_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    agent: BetaManagedAgentsAgent | None = None,
) -> None:
    """Require an admin for an agent setup change."""
    if auth.admin_for(DelegatedAction.CHANNEL_PERMISSIONS) and auth.agent_id is None:
        return
    raise ToolError(
        f"Changing '{agent_name}' needs a workspace or server admin. Ask one to make "
        "the requested change. Do not retry."
    )
