"""The agents a channel's keys name, as the web shows them.

A channel records an agent by one of its names (`agent_pin_names`). The page links each
key to the agent, so every key is answered: an agent that no longer exists maps to an
empty reference, which the page labels "Deleted agent". An agent the caller may not
list is "Private agent": named, with no face and no id to link.
"""

from __future__ import annotations

from collections.abc import Iterable

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from pydantic import BaseModel


class AgentRefJson(BaseModel):
    id: str | None
    """None for an agent that is gone."""
    name: str | None
    avatar: str | None


_GONE = AgentRefJson(id=None, name=None, avatar=None)
_PRIVATE = AgentRefJson(id=None, name="Private agent", avatar=None)


async def agent_refs(
    runtime: WebRuntime, caller: WebCaller, keys: Iterable[str | None]
) -> dict[str, AgentRefJson]:
    """Each key's agent, by key; a key no agent answers to is an empty reference.

    The agents list is the rule: without `agents.view` no agent is shown by name.
    """
    tenant_id = caller.person.tenant_id
    wanted = {key for key in keys if key}
    if not wanted:
        return {}
    found: dict[str, BetaManagedAgentsAgent] = {}
    agents = await listed_agents(runtime, tenant_id)
    # An agent's own names win over the source names a reader variant also answers to.
    for borrowed in (False, True):
        for agent in agents:
            own = (agent.name, agent.metadata.get(MA_METADATA_KEY_NAME))
            for name in agent_pin_names(agent.name, agent.metadata):
                if name in wanted and (borrowed or name in own):
                    found.setdefault(name, agent)
    if "agents.view" not in caller.capabilities:
        return {key: _PRIVATE if key in found else _GONE for key in wanted}
    avatars = await avatars_of(runtime, tenant_id, list({a.id: a for a in found.values()}.values()))
    return {
        key: AgentRefJson(id=agent.id, name=agent.name, avatar=avatars.get(agent.id) or None)
        if (agent := found.get(key)) is not None
        else _GONE
        for key in wanted
    }
