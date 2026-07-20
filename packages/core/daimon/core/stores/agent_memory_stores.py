"""Per-(tenant, agent) MA memory store binding store (agent memory feature).

`insert_memory_store` is the race-safe half of lazy provisioning: two
concurrent first sessions may both create an MA store; ON CONFLICT DO NOTHING
means exactly one row wins, and the return value tells the caller which store
id is canonical (the loser deletes its orphan MA store).
"""

from __future__ import annotations

import uuid

from daimon.core._models import AgentMemoryStore
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_memory_store_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> str | None:
    """Return the bound memstore_... id, or None when unbound."""
    orm = await session.get(AgentMemoryStore, (tenant_id, agent_id))
    return None if orm is None else orm.memory_store_id


async def insert_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    memory_store_id: str,
) -> str:
    """Race-safe insert. Returns the WINNING memory_store_id.

    ON CONFLICT DO NOTHING on the composite PK: when another writer got there
    first, the existing row's id is returned so the caller can discard its own
    just-created MA store.
    """
    stmt = (
        pg_insert(AgentMemoryStore)
        .values(tenant_id=tenant_id, agent_id=agent_id, memory_store_id=memory_store_id)
        .on_conflict_do_nothing(constraint="pk_agent_memory_store")
    )
    await session.execute(stmt)
    await session.flush()
    result = await session.execute(
        select(AgentMemoryStore.memory_store_id).where(
            AgentMemoryStore.tenant_id == tenant_id,
            AgentMemoryStore.agent_id == agent_id,
        )
    )
    return result.scalar_one()


async def clear_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Delete the binding. Idempotent — clearing an absent binding is a no-op
    (the archival path may run against agents that never had memory)."""
    await session.execute(
        delete(AgentMemoryStore).where(
            AgentMemoryStore.tenant_id == tenant_id,
            AgentMemoryStore.agent_id == agent_id,
        )
    )
    await session.flush()
