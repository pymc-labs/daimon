"""The agents a channel's keys name, as the web shows them.

A channel records an agent by one of its names (`agent_pin_names`). The page links each
key to the agent, so every key is answered: an agent that no longer exists maps to an
empty reference, which the page labels "Deleted agent".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_pins import agent_pin_names
from pydantic import BaseModel


class AgentRefJson(BaseModel):
    id: str | None
    """None for an agent that is gone."""
    name: str | None
    avatar: str | None


_GONE = AgentRefJson(id=None, name=None, avatar=None)


async def agent_refs(
    runtime: WebRuntime, tenant_id: uuid.UUID, keys: Iterable[str | None]
) -> dict[str, AgentRefJson]:
    """Each key's agent, by key; a key no agent answers to is an empty reference."""
    wanted = {key for key in keys if key}
    if not wanted:
        return {}
    found: dict[str, BetaManagedAgentsAgent] = {}
    for agent in await listed_agents(runtime, tenant_id):
        for name in agent_pin_names(agent.name, agent.metadata):
            if name in wanted:
                found.setdefault(name, agent)
    avatars = await avatars_of(runtime, tenant_id, list({a.id: a for a in found.values()}.values()))
    return {
        key: AgentRefJson(id=agent.id, name=agent.name, avatar=avatars.get(agent.id) or None)
        if (agent := found.get(key)) is not None
        else _GONE
        for key in wanted
    }
