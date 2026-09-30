"""Channel isolation as the agent tools see it (`daimon.core.channel_isolation`).

A call is inside isolated channel C when the agent executing it is one of C's
own agents (`agent_id` for agent-session tokens, `chat_agent_id` for chat), or
when a tool that knows its turn's location says it runs in C. From inside C a
caller sees only C's own agents; from anywhere else it sees every agent but
those. A tenant that isolates nothing pays one policy read and sees everything.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.channel_isolation import (
    NO_ISOLATION,
    BindingRefusal,
    ChannelIsolation,
    load_channel_isolation,
)
from daimon.core.channel_isolation_setup import render_isolation_refusal
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    skill_owner_candidates,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from fastmcp.exceptions import ToolError

_UNREADABLE_MSG = (
    "This workspace's access policy could not be read, so daimon can't tell which agents "
    "this conversation may reach. Nothing was changed."
)


def agent_name_of(agent: BetaManagedAgentsAgent) -> str:
    return agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name


@dataclass(frozen=True)
class CallerIsolation:
    """The tenant's isolation plus the isolated channel the call runs in, if any."""

    isolation: ChannelIsolation
    inside_channel_id: str | None = None

    def sees(self, agent_name: str) -> bool:
        return self.isolation.is_visible(agent_name, inside_channel_id=self.inside_channel_id)

    def sees_agent(self, agent: BetaManagedAgentsAgent) -> bool:
        return self.sees(agent_name_of(agent))

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


async def load_skill_owners(
    runtime: McpRuntime, caller: CallerIsolation, tenant_id: uuid.UUID
) -> SkillOwners:
    """The skill owners `caller.hides_skill` needs; nothing is read while nothing is isolated."""
    if not caller.isolation.is_active:
        return NO_SKILL_OWNERS
    async with runtime.session_factory() as session:
        rows = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    agents = await list_agents_by_tenant(runtime.client, tenant_id=tenant_id)
    return SkillOwners(
        {row.anthropic_id: row.agent_name for row in rows if row.anthropic_id},
        frozenset(map(agent_name_of, agents)) | caller.isolation.agent_channel_ids.keys(),
    )


OPEN_ISOLATION = CallerIsolation(NO_ISOLATION)


def refuse(refusal: BindingRefusal | None, *, agent_name: str | None) -> None:
    if refusal is not None:
        message = render_isolation_refusal(refusal, agent_name=agent_name)
        raise ToolError(f"{message} Nothing was changed.")


def require_bindable(
    isolation: ChannelIsolation,
    agent_name: str,
    *,
    agent: BetaManagedAgentsAgent | None,
    channel_id: str | None,
) -> None:
    """Refuse routing `agent_name` at `channel_id` (None: tenant default; a thread: its parent)."""
    managed = agent is not None and agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    refusal = isolation.binding_refusal(
        agent_name, channel_id=channel_id, is_daimon_managed=managed
    )
    refuse(refusal, agent_name=agent_name)


async def load_isolation(runtime: McpRuntime, tenant_id: uuid.UUID) -> ChannelIsolation:
    """The tenant's isolation; an unreadable policy refuses the call rather than fall open."""
    try:
        async with runtime.session_factory() as session:
            return await load_channel_isolation(
                session, tenant_id=tenant_id, default=runtime.deployment_default
            )
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
    (a thread's parent) when the tool knows the turn's channel."""
    isolation = await load_isolation(runtime, auth.tenant_id)
    if not isolation.is_active:
        return OPEN_ISOLATION
    inside = isolation.isolated_channel(location_channel_id)
    executing = auth.agent_id or auth.chat_agent_id
    if inside is None and executing is not None:
        if agents is None:
            agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
        agent = next(
            (
                agent
                for agent in agents
                if derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id) == executing
            ),
            None,
        )
        inside = isolation.channel_of(agent_name_of(agent)) if agent is not None else None
    return CallerIsolation(isolation, inside)


__all__ = [
    "NO_SKILL_OWNERS",
    "OPEN_ISOLATION",
    "CallerIsolation",
    "SkillOwners",
    "agent_name_of",
    "load_caller_isolation",
    "load_isolation",
    "load_skill_owners",
    "refuse",
    "require_bindable",
]
