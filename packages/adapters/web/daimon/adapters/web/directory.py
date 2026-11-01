"""The workspace's agents, and which of them a person may chat with on the web."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import Role, is_admin
from daimon.core.authz import Action, Place, Surface, authorize, build_agent_ref
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.stores import web_agent_grants, web_invites
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import WebPersonRow
from daimon.core.turn_hold import TurnHold
from daimon.core.web_access import may_chat
from daimon.core.web_identity import WebPrincipal, place_of
from sqlalchemy.ext.asyncio import AsyncSession

#: How long a workspace's agent list is reused before asking again.
AGENTS_TTL_S = 60.0


@dataclass
class AgentDirectory:
    """Each workspace's agents, listed at most once a minute."""

    _cache: dict[uuid.UUID, tuple[float, list[BetaManagedAgentsAgent]]] = field(
        default_factory=dict[uuid.UUID, tuple[float, list[BetaManagedAgentsAgent]]]
    )

    async def agents(
        self, anthropic: AsyncAnthropic, tenant_id: uuid.UUID
    ) -> list[BetaManagedAgentsAgent]:
        cached = self._cache.get(tenant_id)
        if cached is None or time.monotonic() - cached[0] > AGENTS_TTL_S:
            cached = (time.monotonic(), await list_agents_by_tenant(anthropic, tenant_id=tenant_id))
            self._cache[tenant_id] = cached
        return cached[1]

    def forget(self, tenant_id: uuid.UUID) -> None:
        """Drop a workspace's list, so an agent changed here shows on the next read."""
        self._cache.pop(tenant_id, None)

    def cached(self, tenant_id: uuid.UUID, agent_id: str) -> BetaManagedAgentsAgent | None:
        """An agent already listed, or None; never asks."""
        cached = self._cache.get(tenant_id)
        return next((a for a in cached[1] if a.id == agent_id), None) if cached else None


async def chattable(
    db: AsyncSession,
    agents: list[BetaManagedAgentsAgent],
    *,
    person: WebPersonRow,
    role: Role,
    principal: WebPrincipal,
) -> list[BetaManagedAgentsAgent]:
    """The agents a person may chat with: allowed by the access policy, under a visitor's
    hold, and by each agent's web grant."""
    tenant_id = person.tenant_id
    policy = await load_access_policy(db, tenant_id=tenant_id)
    grants = await web_agent_grants.list_grants(db, tenant_id=tenant_id)
    invited = (
        await web_invites.invited_agent_ids(db, tenant_id=tenant_id, invite_id=person.invite_id)
        if person.invite_id is not None
        else frozenset[str]()
    )
    visitor = person.kind == "visitor"
    place = place_of(person.id)
    return [
        agent
        for agent in agents
        if may_chat(
            agent.id,
            grants.get(agent.id),
            is_visitor=visitor,
            is_admin=is_admin(role),
            groups=principal.groups,
            invited_agent_ids=invited,
        )
        and authorize(
            policy,
            subject=principal.subject,
            action=Action.RUN_AGENT,
            surface=Surface.WEB,
            agent=build_agent_ref(agent.name, agent.metadata),
            place=Place(channel_id=place, parent_channel_id=place),
            hold=TurnHold("web", place, client=True) if visitor else None,
        )
    ]
