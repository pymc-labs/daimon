"""Agents an eval arm may run: the workspace's own, as the isolation rules let a panel see them."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.evals.format import JsonObject
from daimon.core.routine_service import isolation_view_loader
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AgentLookup = Callable[[uuid.UUID, str], Awaitable[JsonObject | None]]
"""The config of a workspace's agent by id or name, or None when there is none to see."""

_CONFIG = ("model", "system", "tools", "mcp_servers", "skills", "multiagent")
_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def agent_lookup(
    sessions: async_sessionmaker[AsyncSession], anthropic: AsyncAnthropic
) -> AgentLookup:
    async def lookup(tenant_id: uuid.UUID, ref: str) -> JsonObject | None:
        view = await isolation_view_loader(sessions, anthropic, tenant_id)()
        agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id, records=sessions)
        for agent in agents:
            if ref in (agent.id, agent.name) and view.viewer.sees_agent(agent):
                return _OBJECT.validate_python(agent.model_dump(mode="json", include=set(_CONFIG)))
        return None

    return lookup
