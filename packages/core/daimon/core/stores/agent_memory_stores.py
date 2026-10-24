"""MA memory store bindings (agent memory feature).

The shared store is one per (tenant, agent); a personal store is one per
(tenant, agent, account); a channel store is one per (tenant, agent, platform,
parent channel) and is what a held turn mounts instead of the other two.

`insert_memory_store` is the race-safe half of lazy provisioning: two
concurrent first sessions may both create an MA store; ON CONFLICT DO NOTHING
means exactly one row wins, and the return value tells the caller which store
id is canonical (the loser deletes its orphan MA store).
"""

from __future__ import annotations

import uuid

from daimon.core._models import (
    Account,
    AgentChannelMemoryStore,
    AgentMemoryStore,
    AgentPersonalMemoryStore,
)
from daimon.core.errors import StoreError
from sqlalchemy import Select, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import Insert
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
    """Race-safe insert. Returns the WINNING memory_store_id (`_insert_winner`)."""
    stmt = (
        pg_insert(AgentMemoryStore)
        .values(tenant_id=tenant_id, agent_id=agent_id, memory_store_id=memory_store_id)
        .on_conflict_do_nothing(constraint="pk_agent_memory_store")
    )
    return await _insert_winner(
        session,
        stmt,
        select(AgentMemoryStore.memory_store_id).where(
            AgentMemoryStore.tenant_id == tenant_id,
            AgentMemoryStore.agent_id == agent_id,
        ),
        binding=f"tenant={tenant_id} agent={agent_id}",
    )


async def _insert_winner(
    session: AsyncSession,
    stmt: Insert,
    winner: Select[tuple[str]],
    *,
    binding: str,
) -> str:
    """Run an ON CONFLICT DO NOTHING binding insert; return the store id `winner` selects.

    When another writer got there first, the existing row's id is returned so
    the caller can discard its own just-created MA store.

    Bounded retry (3 attempts): the no-op insert and the follow-up read are
    two separate statements, so a concurrent transaction can delete the row in
    between — the read then finds nothing rather than the expected winner.
    Rather than let that surface as an unhandled NoResultFound, we retry the
    insert (with the row now gone, our insert should win) up to `max_attempts`
    times. If the row still can't be resolved after all attempts (pathological
    concurrent churn), raise StoreError.
    """
    max_attempts = 3
    for _ in range(max_attempts):
        await session.execute(stmt)
        await session.flush()
        found = (await session.execute(winner)).scalar_one_or_none()
        if found is not None:
            return found
    raise StoreError(
        f"memory store binding {binding} survived no insert in {max_attempts} attempts — "
        "concurrent clearing outpaced the retry budget"
    )


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


# --- Personal stores: one per (tenant, agent, account) -----------------------


async def get_personal_memory_store_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> str | None:
    """Return the person's mountable memstore_... id for this agent, or None.

    An archived row (the agent was archived) is never mounted: None.
    """
    orm = await session.get(AgentPersonalMemoryStore, (tenant_id, agent_id, account_id))
    return None if orm is None or orm.archived_at is not None else orm.memory_store_id


async def insert_personal_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    memory_store_id: str,
) -> str:
    """Race-safe insert. Returns the WINNING memory_store_id (`_insert_winner`)."""
    stmt = (
        pg_insert(AgentPersonalMemoryStore)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            memory_store_id=memory_store_id,
        )
        .on_conflict_do_nothing(constraint="pk_agent_personal_memory_store")
    )
    return await _insert_winner(
        session,
        stmt,
        select(AgentPersonalMemoryStore.memory_store_id).where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
            AgentPersonalMemoryStore.account_id == account_id,
            AgentPersonalMemoryStore.archived_at.is_(None),
        ),
        binding=f"tenant={tenant_id} agent={agent_id} account={account_id}",
    )


async def list_personal_memory_stores_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str]]:
    """Every `(account_id, memory_store_id)` bound for one agent and not yet archived."""
    rows = await session.execute(
        select(AgentPersonalMemoryStore.account_id, AgentPersonalMemoryStore.memory_store_id).where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
            AgentPersonalMemoryStore.archived_at.is_(None),
        )
    )
    return [(account_id, store_id) for account_id, store_id in rows.tuples()]


async def list_personal_memory_stores_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Every `(tenant_id, agent_id, memory_store_id)` one person has, across tenants,
    archived ones included: this is the erasure inventory."""
    rows = await session.execute(
        select(
            AgentPersonalMemoryStore.tenant_id,
            AgentPersonalMemoryStore.agent_id,
            AgentPersonalMemoryStore.memory_store_id,
        ).where(AgentPersonalMemoryStore.account_id == account_id)
    )
    return [(tenant_id, agent_id, store_id) for tenant_id, agent_id, store_id in rows.tuples()]


async def mark_personal_memory_store_archived(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Stamp a binding archived: never mounted again, still found by erasure."""
    await session.execute(
        update(AgentPersonalMemoryStore)
        .where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
            AgentPersonalMemoryStore.account_id == account_id,
        )
        .values(archived_at=func.now())
    )
    await session.flush()


async def list_erased_accounts_with_personal_memory(session: AsyncSession) -> list[uuid.UUID]:
    """Accounts whose row is gone but who still have personal stores to delete.

    The bindings carry no FK to `accounts`, so an erasure whose upstream delete
    failed, or ran without an Anthropic client, leaves exactly these behind.
    """
    rows = await session.execute(
        select(AgentPersonalMemoryStore.account_id)
        .where(~exists().where(Account.id == AgentPersonalMemoryStore.account_id))
        .distinct()
    )
    return list(rows.scalars())


async def clear_personal_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Delete one person's binding. Idempotent."""
    await session.execute(
        delete(AgentPersonalMemoryStore).where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
            AgentPersonalMemoryStore.account_id == account_id,
        )
    )
    await session.flush()


# --- Channel stores: one per (tenant, agent, platform, parent channel) -------


async def get_channel_memory_store_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> str | None:
    """Return the held channel's memstore_... id for this agent, or None when unbound."""
    orm = await session.get(AgentChannelMemoryStore, (tenant_id, agent_id, platform, channel_id))
    return None if orm is None else orm.memory_store_id


async def insert_channel_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    channel_id: str,
    memory_store_id: str,
) -> str:
    """Race-safe insert. Returns the WINNING memory_store_id (`_insert_winner`)."""
    stmt = (
        pg_insert(AgentChannelMemoryStore)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            platform=platform,
            channel_id=channel_id,
            memory_store_id=memory_store_id,
        )
        .on_conflict_do_nothing(constraint="pk_agent_channel_memory_store")
    )
    return await _insert_winner(
        session,
        stmt,
        select(AgentChannelMemoryStore.memory_store_id).where(
            AgentChannelMemoryStore.tenant_id == tenant_id,
            AgentChannelMemoryStore.agent_id == agent_id,
            AgentChannelMemoryStore.platform == platform,
            AgentChannelMemoryStore.channel_id == channel_id,
        ),
        binding=f"tenant={tenant_id} agent={agent_id} {platform}:{channel_id}",
    )


async def list_channel_memory_stores_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[tuple[str, str, str]]:
    """Every `(platform, channel_id, memory_store_id)` bound for one agent."""
    rows = await session.execute(
        select(
            AgentChannelMemoryStore.platform,
            AgentChannelMemoryStore.channel_id,
            AgentChannelMemoryStore.memory_store_id,
        )
        .where(
            AgentChannelMemoryStore.tenant_id == tenant_id,
            AgentChannelMemoryStore.agent_id == agent_id,
        )
        .order_by(AgentChannelMemoryStore.platform, AgentChannelMemoryStore.channel_id)
    )
    return [(platform, channel_id, store_id) for platform, channel_id, store_id in rows.tuples()]


async def list_channel_memory_stores(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, uuid.UUID, str, str, str]]:
    """Every `(tenant_id, agent_id, platform, channel_id, store_id)`, optionally for one tenant."""
    stmt = select(
        AgentChannelMemoryStore.tenant_id,
        AgentChannelMemoryStore.agent_id,
        AgentChannelMemoryStore.platform,
        AgentChannelMemoryStore.channel_id,
        AgentChannelMemoryStore.memory_store_id,
    ).order_by(
        AgentChannelMemoryStore.tenant_id,
        AgentChannelMemoryStore.agent_id,
        AgentChannelMemoryStore.platform,
        AgentChannelMemoryStore.channel_id,
    )
    if tenant_id is not None:
        stmt = stmt.where(AgentChannelMemoryStore.tenant_id == tenant_id)
    rows = await session.execute(stmt)
    return [(t, a, p, c, s) for t, a, p, c, s in rows.tuples()]


async def clear_channel_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> None:
    """Delete one channel's binding. Idempotent."""
    await session.execute(
        delete(AgentChannelMemoryStore).where(
            AgentChannelMemoryStore.tenant_id == tenant_id,
            AgentChannelMemoryStore.agent_id == agent_id,
            AgentChannelMemoryStore.platform == platform,
            AgentChannelMemoryStore.channel_id == channel_id,
        )
    )
    await session.flush()


async def list_memory_stores(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Every shared `(tenant_id, agent_id, memory_store_id)`, optionally for one tenant."""
    stmt = select(
        AgentMemoryStore.tenant_id, AgentMemoryStore.agent_id, AgentMemoryStore.memory_store_id
    ).order_by(AgentMemoryStore.tenant_id, AgentMemoryStore.agent_id)
    if tenant_id is not None:
        stmt = stmt.where(AgentMemoryStore.tenant_id == tenant_id)
    rows = await session.execute(stmt)
    return [(t, a, s) for t, a, s in rows.tuples()]
