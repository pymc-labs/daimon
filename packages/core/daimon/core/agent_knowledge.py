"""What an agent remembers, read-only: its shared memory, and its memory in each channel.

Shared memory is for an admin or the agent's owner. Channel memory is for admins only,
as a channel's own memory may come from a place the owner cannot see. A store is named
here by a key, never by its id, and is always looked up from the agent, so no request
can read another agent's memory.
"""

from __future__ import annotations

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import AgentTarget, load_target, require_setup
from daimon.core.memory_view import get_memory_content, list_memory_paths
from daimon.core.stores.agent_memory_stores import (
    get_channel_memory_store_id,
    get_memory_store_id,
    list_channel_memory_stores_for_agent,
)
from daimon.core.stores.platform_names import get_channel_names
from pydantic import BaseModel, ConfigDict

SHARED = "shared"


class MemoryPlace(BaseModel):
    model_config = ConfigDict(frozen=True)

    store: str
    platform: str
    name: str


class KnowledgeStores(BaseModel):
    model_config = ConfigDict(frozen=True)

    shared: bool
    channels: list[MemoryPlace]


def channel_key(platform: str, channel_id: str) -> str:
    return f"{platform}/{channel_id}"


async def _store_id(deps: AgentEditDeps, target: AgentTarget, actor: AgentActor, store: str) -> str:
    """The memory store id `store` names on this agent, or not found."""
    async with deps.session_factory() as session:
        if store == SHARED:
            found = await get_memory_store_id(
                session, tenant_id=actor.tenant_id, agent_id=target.agent_id
            )
        else:
            platform, _, channel_id = store.partition("/")
            if not platform or not channel_id:
                raise AgentEditError("No such memory.", kind="not_found")
            if not actor.subject.is_admin:
                raise AgentEditError("Channel memory is for admins.", kind="forbidden")
            found = await get_channel_memory_store_id(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent_id,
                platform=platform,
                channel_id=channel_id,
            )
    if found is None:
        raise AgentEditError("No such memory.", kind="not_found")
    return found


async def list_stores(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str
) -> KnowledgeStores:
    """Which memory the agent has: its shared store, and (admins) each channel's."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Memory")
    channels: list[MemoryPlace] = []
    async with deps.session_factory() as session:
        shared = (
            await get_memory_store_id(session, tenant_id=actor.tenant_id, agent_id=target.agent_id)
            is not None
        )
        if actor.subject.is_admin:
            rows = await list_channel_memory_stores_for_agent(
                session, tenant_id=actor.tenant_id, agent_id=target.agent_id
            )
            for platform in sorted({p for p, _, _ in rows}):
                ids = [c for p, c, _ in rows if p == platform]
                names = await get_channel_names(
                    session, tenant_id=actor.tenant_id, platform=platform, channel_ids=ids
                )
                channels += [
                    MemoryPlace(
                        store=channel_key(platform, c), platform=platform, name=names.get(c, c)
                    )
                    for c in sorted(ids)
                ]
    return KnowledgeStores(shared=shared, channels=channels)


async def list_paths(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str, store: str
) -> list[str]:
    """The paths of every memory in `store`."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Memory")
    store_id = await _store_id(deps, target, actor, store)
    return await list_memory_paths(deps.client, store_id, scope=actor.scope())


async def read(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str, store: str, path: str
) -> str:
    """The text of the memory at `path` in `store`."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Memory")
    store_id = await _store_id(deps, target, actor, store)
    content = await get_memory_content(deps.client, store_id, path, scope=actor.scope())
    if content is None:
        raise AgentEditError("No such memory.", kind="not_found")
    return content
