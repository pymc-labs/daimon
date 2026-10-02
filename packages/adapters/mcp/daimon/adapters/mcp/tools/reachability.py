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

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._ctx import _require_admin  # pyright: ignore[reportPrivateUsage]
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL, agent_pin_names
from daimon.core.agent_reach import load_target_facts, may_bind_as_channel_default
from daimon.core.authz import Action, AgentRef, Place, authorize
from daimon.core.channel_admins import ChannelAdminCaller, is_channel_admin
from daimon.core.operation_policy import OperationKind, TargetFacts, decide_operation
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.channel_admins import get_channel_admins
from fastmcp.exceptions import ToolError

REACHABILITY_GATED_FIELDS: Final[frozenset[str]] = frozenset(
    {"system", "model", "skills", "mcp_servers", "tools"}
)

UNPLACED_RUN_REASON: Final[str] = (
    "has other people's conversations or routines whose channel is unknown, so they could "
    "be in any channel"
)
"""Refusal wording for `TargetFacts.has_unplaced_run`, after the agent's name."""


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
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    is_daimon_managed: bool,
) -> TargetFacts:
    """Policy facts for the agent carrying `agent_names` in the caller's own tenant."""
    async with runtime.session_factory() as session:
        return await load_target_facts(
            session,
            operation,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            agent_names=agent_names,
            ma_agent_id=ma_agent_id,
            default=runtime.deployment_default,
            caller=channel_admin_caller(auth),
            is_daimon_managed=is_daimon_managed,
            caller_account_id=auth.account_id,
            caller_platform_user_id=auth.platform_user_id,
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


async def require_scope_admin(
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str | None
) -> None:
    """The workspace scope needs a server admin; a channel's also admits its channel admins."""
    if channel_id is None:
        _require_admin(auth)
    else:
        await require_channel_admin(runtime, auth, channel_id=channel_id)


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
    """Raise ``ToolError`` when the agent is pinned elsewhere, or a channel admin
    binds another channel's own agent."""
    names = tuple(name for name in _target_names(agent_name, agent) if name)
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
        # Nobody, server admins included: the agent would refuse every turn here.
        decision = authorize(
            policy,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
            action=Action.BIND_CHANNEL_DEFAULT,
            agent=AgentRef.of(*names),
            place=Place(channel_id=channel_id),
        )
        if decision.reason == "channel_isolated":
            raise ToolError(
                "This channel is isolated, so only its own agents (pinned to it alone) can be "
                f"its default, and '{agent_name}' is not one. Nothing was changed. Pick one of "
                "its own agents, or ask a server admin. Do not retry."
            )
        if not decision:
            raise ToolError(
                f"An operator pinned '{agent_name}' to other channels, so it would refuse "
                "every turn here and cannot be this channel's default. Nothing was changed. "
                "Pick another agent, or ask an operator to change the pin. Do not retry."
            )
        if await may_bind_as_channel_default(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            agent_names=names,
            ma_agent_id=str(agent.id) if agent is not None else None,
            default=runtime.deployment_default,
            caller=channel_admin_caller(auth),
            is_daimon_managed=is_daimon_managed,
            caller_account_id=auth.account_id,
        ):
            return
    raise ToolError(
        f"'{agent_name}' answers in channels this caller does not administer, has other "
        "people's conversations or routines whose channel is unknown, or runs unattended for "
        "someone with wider rights, so only a workspace or server admin can "
        "make it this channel's default. A channel admin may "
        "pick a built-in agent, the workspace default, an agent that answers nowhere yet, "
        "or one that answers only in their channels. Nothing was changed. Do not retry."
    )


async def require_admin_for_reachable_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    agent: BetaManagedAgentsAgent | None = None,
) -> None:
    """Raise ``ToolError`` if a non-admin caller is patching a currently-reachable agent.

    Returns immediately for an admin caller without touching the database.
    For a non-admin caller, reads the tenant's config cascade and raises when
    ``agent_name`` currently resolves for any user in this tenant (channel,
    tenant, or deployment tier), unless the caller administers every channel
    the agent answers in. ``tenant_id`` always comes from ``auth.tenant_id`` —
    never from a caller-supplied parameter — so a caller cannot point the read
    at another tenant's config rows. The decision itself is
    `daimon.core.operation_policy.decide_operation`'s. Pass the resolved
    ``agent`` so every name it carries and its bound threads count.
    """
    if auth.is_admin:
        return
    facts = await target_facts(
        runtime,
        auth,
        "agent_spec_edit",
        agent_names=_target_names(agent_name, agent),
        ma_agent_id=str(agent.id) if agent is not None else None,
        is_daimon_managed=False,
    )
    outcome = decide_operation("agent_spec_edit", is_admin=False, target=facts)
    if outcome == "needs_admin":
        if facts.runs_unattended_beyond_caller:
            why = (
                "runs unattended (a routine or queued wake) for someone with wider rights "
                "than this caller"
            )
        elif facts.has_unplaced_run:
            why = UNPLACED_RUN_REASON
        else:
            why = (
                "is currently a default agent here and answers or runs outside the channels "
                "this caller administers"
            )
        raise ToolError(
            f"'{agent_name}' {why}, so an admin must change its setup. Tell the caller to "
            f"ask a workspace or server admin to make the requested change to '{agent_name}'; "
            "carry the "
            "specific action from the conversation into that handoff. Do not retry. "
            "Creating or forking your own agent is not gated."
        )
