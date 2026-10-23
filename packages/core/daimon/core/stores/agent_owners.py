"""Persistence for agent owners; callers own the transaction and the authorization."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core._models import Account, AgentOwner, PlatformPrincipal
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

type OwnerSource = Literal["created", "admin_assigned"]


async def owner_of(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, *, lock: bool = False
) -> uuid.UUID | None:
    """The agent's owner account, if it has one in this tenant.

    `lock` holds the row (and the owner's account) until the transaction ends,
    so a write authorized by ownership cannot interleave with an owner change.
    """
    query = (
        select(AgentOwner.owner_account_id)
        .join(Account, Account.id == AgentOwner.owner_account_id)
        .where(
            AgentOwner.tenant_id == tenant_id,
            AgentOwner.agent_id == agent_id,
            Account.tenant_id == tenant_id,
        )
    )
    if lock:
        query = query.with_for_update(of=AgentOwner, read=True)
    return await session.scalar(query)


async def set_owner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    source: OwnerSource,
) -> None:
    """Record or replace the agent's owner. The account must belong to the tenant."""
    belongs = await session.scalar(
        select(Account.id).where(Account.id == owner_account_id, Account.tenant_id == tenant_id)
    )
    if belongs is None:
        raise LookupError("owner account is not in this tenant")
    statement = insert(AgentOwner).values(
        tenant_id=tenant_id, agent_id=agent_id, owner_account_id=owner_account_id, source=source
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="pk_agent_owner",
            set_={
                "owner_account_id": statement.excluded.owner_account_id,
                "source": statement.excluded.source,
                "recorded_at": statement.excluded.recorded_at,
            },
        )
    )


async def record_creator(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    """Record the account that created the agent, unless it already has an owner.

    The account must belong to the tenant, as for `set_owner`.
    """
    belongs = await session.scalar(
        select(Account.id).where(Account.id == account_id, Account.tenant_id == tenant_id)
    )
    if belongs is None:
        raise LookupError("owner account is not in this tenant")
    result = await session.execute(
        insert(AgentOwner)
        .values(
            tenant_id=tenant_id, agent_id=agent_id, owner_account_id=account_id, source="created"
        )
        .on_conflict_do_nothing(constraint="pk_agent_owner")
        .returning(AgentOwner.agent_id)
    )
    return result.scalar_one_or_none() is not None


async def clear_owner(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(AgentOwner)
        .where(AgentOwner.tenant_id == tenant_id, AgentOwner.agent_id == agent_id)
        .returning(AgentOwner.agent_id)
    )
    return result.scalar_one_or_none() is not None


async def owned_by(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Every agent id the account owns in this tenant."""
    rows = await session.scalars(
        select(AgentOwner.agent_id).where(
            AgentOwner.tenant_id == tenant_id, AgentOwner.owner_account_id == account_id
        )
    )
    return frozenset(rows)


async def has_foreign_principal(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    """Whether any of the account's platform identities belongs to another organisation."""
    return (
        await session.scalar(
            select(PlatformPrincipal.id)
            .where(
                PlatformPrincipal.tenant_id == tenant_id,
                PlatformPrincipal.account_id == account_id,
                PlatformPrincipal.foreign_home_id.is_not(None),
            )
            .limit(1)
        )
        is not None
    )
