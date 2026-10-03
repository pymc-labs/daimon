"""Channel isolation as the agent tools see it (`daimon.core.channel_isolation`).

A call is inside isolated channel C when an agent key is bound to C
(`token_channel_id`), when a tool that knows its turn's location from a
verified origin says it runs in C, or when a chat turn's agent is one of C's
own. An agent key is never inside by its agent alone. From inside C a caller
sees only C's own agents; from anywhere else it sees every agent but those. A
tenant that isolates nothing pays one policy read and sees everything.
What an agent may run, post or read is `daimon.core.authz`'s to decide.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    isolated_channel_of,
    isolation_owner,
)
from daimon.core.agent_pins import agent_aliases, agent_pin_names
from daimon.core.channel_environments import load_hidden_environment_names
from daimon.core.channel_isolation import BindingRefusal, IsolationViewer, binding_refusal
from daimon.core.channel_isolation_setup import render_isolation_refusal
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import skill_owner_candidates
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from fastmcp.exceptions import ToolError

_UNREADABLE_MSG = (
    "This workspace's access policy could not be read, so daimon can't tell which agents "
    "this conversation may reach. Nothing was changed."
)


@dataclass(frozen=True)
class CallerIsolation(IsolationViewer):
    """The tenant's policy plus the isolated channel the call runs in, if any."""

    def isolated_place(
        self, channel_id: str | None, parent_channel_id: str | None = None
    ) -> str | None:
        return isolated_channel_of(self.policy, channel_id, parent_channel_id)

    def hides_skill(self, owners: SkillOwners, *, skill_id: str, body: str) -> bool:
        """Whether a tenant skill may belong to an agent this caller can't see."""
        return not all(map(self.sees, owners.of(skill_id, body)))


@dataclass(frozen=True)
class SkillOwners:
    """Who each agent-scoped skill belongs to: its upload row, else its title."""

    by_skill_id: Mapping[str, str]
    agent_names: frozenset[str]

    def of(self, skill_id: str, body: str) -> frozenset[str]:
        return skill_owner_candidates(
            body, stored_owner=self.by_skill_id.get(skill_id), agent_names=self.agent_names
        )


NO_SKILL_OWNERS = SkillOwners({}, frozenset())
OPEN_ISOLATION = CallerIsolation(OPEN_ACCESS_POLICY)


async def load_skill_owners(
    runtime: McpRuntime, caller: CallerIsolation, tenant_id: uuid.UUID
) -> SkillOwners:
    """The skill owners `caller.hides_skill` needs; nothing is read while nothing is isolated."""
    if not caller.is_active:
        return NO_SKILL_OWNERS
    async with runtime.session_factory() as session:
        rows = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    agents = await list_agents_by_tenant(runtime.client, tenant_id=tenant_id)
    names = {name for agent in agents for name in agent_pin_names(agent.name, agent.metadata)}
    return SkillOwners(
        {row.anthropic_id: row.agent_name for row in rows if row.anthropic_id},
        frozenset(name for name in names if name) | caller.policy.agent_channel_pins.keys(),
    )


def refuse(refusal: BindingRefusal | None, *, agent_name: str | None) -> None:
    if refusal is not None:
        message = render_isolation_refusal(refusal, agent_name=agent_name)
        raise ToolError(f"{message} Nothing was changed.")


def require_bindable(
    policy: TenantAccessPolicy,
    agent_name: str,
    *,
    agent: BetaManagedAgentsAgent | None,
    channel_id: str | None,
    parent_channel_id: str | None = None,
) -> None:
    """Refuse routing `agent_name` at `channel_id` (None: tenant default) for a pin or isolation.

    A thread binding passes its parent channel too.
    """
    names = (agent_name, *(agent_pin_names(agent.name, agent.metadata) if agent else ()))
    refuse(
        binding_refusal(
            policy, agent_names=names, channel_id=channel_id, parent_channel_id=parent_channel_id
        ),
        agent_name=agent_name,
    )


async def load_isolation(runtime: McpRuntime, tenant_id: uuid.UUID) -> TenantAccessPolicy:
    """The tenant's policy; an unreadable one refuses the call rather than fall open."""
    try:
        async with runtime.session_factory() as session:
            return await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable as exc:
        raise ToolError(_UNREADABLE_MSG) from exc


async def load_caller_isolation(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agents: Sequence[BetaManagedAgentsAgent] | None = None,
    location_channel_id: str | None = None,
) -> CallerIsolation:
    """Where the caller stands. Pass `agents` when already listed; `location_channel_id`
    (a thread's parent) when the tool knows the turn's channel from its verified origin.

    A chat turn's own agent counts (only an isolated channel's own agents run
    there); an agent key's does not, and neither does a location, unless the
    key is bound to the channel. A chat turn whose agent can't be found is
    refused while any channel is isolated.
    """
    policy = await load_isolation(runtime, auth.tenant_id)
    if not policy.isolated_channel_ids:
        return OPEN_ISOLATION
    if agents is None:
        agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    if auth.agent_id is not None:
        location_channel_id = None
    inside = isolated_channel_of(policy, location_channel_id) or isolated_channel_of(
        policy, token_channel_id(auth)
    )
    if inside is None and auth.chat_agent_id is not None:
        agent = next(
            (
                agent
                for agent in agents
                if derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id)
                == auth.chat_agent_id
            ),
            None,
        )
        if agent is None:
            # It may be an isolated channel's own agent: judged from outside, it
            # would see and reach past its channel.
            raise ToolError(
                "this conversation's agent could not be found, so daimon can't tell "
                "which channel it is held to. Nothing was done. Tell the caller."
            )
        inside = isolation_owner(policy, agent_pin_names(agent.name, agent.metadata))
    return CallerIsolation(policy, inside, agent_aliases(agents))


async def load_caller_hidden_environments(
    runtime: McpRuntime, auth: AuthIdentity
) -> frozenset[str]:
    """Environment names only isolated channels across the caller's line pick; an operator
    sees all. Tools treat them as missing."""
    if auth.is_operator:
        return frozenset()
    caller = await load_caller_isolation(runtime, auth)
    async with runtime.session_factory() as session:
        return await load_hidden_environment_names(
            session, tenant_id=auth.tenant_id, viewer=caller, default=runtime.deployment_default
        )


__all__ = [
    "NO_SKILL_OWNERS",
    "OPEN_ISOLATION",
    "CallerIsolation",
    "SkillOwners",
    "load_caller_hidden_environments",
    "load_caller_isolation",
    "load_isolation",
    "load_skill_owners",
    "refuse",
    "require_bindable",
]
