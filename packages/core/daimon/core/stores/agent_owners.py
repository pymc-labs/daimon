"""Persistence for agent owners; callers own the transaction and the authorization."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Literal

from daimon.core._models import Account, AgentCoOwner, AgentOwner, PlatformPrincipal
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

type OwnerSource = Literal["created", "admin_assigned"]


class ForeignOwnerError(ValueError):
    """The account belongs to a person from another organisation, who can never own an agent."""


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
    """Every agent id the account owns in this tenant, as primary owner or co-owner."""
    primary = await session.scalars(
        select(AgentOwner.agent_id).where(
            AgentOwner.tenant_id == tenant_id, AgentOwner.owner_account_id == account_id
        )
    )
    co_owned = await session.scalars(
        select(AgentCoOwner.agent_id).where(
            AgentCoOwner.tenant_id == tenant_id, AgentCoOwner.account_id == account_id
        )
    )
    return frozenset(primary) | frozenset(co_owned)


async def is_owner(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    lock: bool = False,
) -> bool:
    """Whether the account is the agent's primary owner or one of its co-owners.

    A person from another organisation (`foreign_home_id`) is never an owner,
    whichever row names them. `lock` holds the owner rows read until the
    transaction ends, as for `owner_of`.
    """
    if await has_foreign_principal(session, tenant_id, account_id):
        return False
    if await owner_of(session, tenant_id, agent_id, lock=lock) == account_id:
        return True
    query = (
        select(AgentCoOwner.account_id)
        .join(Account, Account.id == AgentCoOwner.account_id)
        .where(
            AgentCoOwner.tenant_id == tenant_id,
            AgentCoOwner.agent_id == agent_id,
            AgentCoOwner.account_id == account_id,
            Account.tenant_id == tenant_id,
        )
    )
    if lock:
        query = query.with_for_update(of=AgentCoOwner, read=True)
    return await session.scalar(query) is not None


async def co_owners_of(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[uuid.UUID]:
    """The agent's co-owner accounts in this tenant, oldest addition first."""
    rows = await session.scalars(
        select(AgentCoOwner.account_id)
        .join(Account, Account.id == AgentCoOwner.account_id)
        .where(
            AgentCoOwner.tenant_id == tenant_id,
            AgentCoOwner.agent_id == agent_id,
            Account.tenant_id == tenant_id,
        )
        .order_by(AgentCoOwner.added_at, AgentCoOwner.account_id)
    )
    return list(rows)


async def add_co_owner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    added_by_account_id: uuid.UUID | None,
) -> bool:
    """Make the account a co-owner; false when it already owns the agent in either role.

    The account must belong to the tenant (`LookupError`) and must not be a
    person from another organisation (`ForeignOwnerError`).
    """
    belongs = await session.scalar(
        select(Account.id).where(Account.id == account_id, Account.tenant_id == tenant_id)
    )
    if belongs is None:
        raise LookupError("owner account is not in this tenant")
    if await has_foreign_principal(session, tenant_id, account_id):
        raise ForeignOwnerError("a person from another organisation cannot own an agent")
    if await owner_of(session, tenant_id, agent_id) == account_id:
        return False
    result = await session.execute(
        insert(AgentCoOwner)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            added_by_account_id=added_by_account_id,
        )
        .on_conflict_do_nothing(constraint="pk_agent_co_owners")
        .returning(AgentCoOwner.agent_id)
    )
    return result.scalar_one_or_none() is not None


async def remove_co_owner(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(AgentCoOwner)
        .where(
            AgentCoOwner.tenant_id == tenant_id,
            AgentCoOwner.agent_id == agent_id,
            AgentCoOwner.account_id == account_id,
        )
        .returning(AgentCoOwner.agent_id)
    )
    return result.scalar_one_or_none() is not None


async def owner_identities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_ids: Collection[uuid.UUID],
    *,
    platform: str,
) -> dict[uuid.UUID, tuple[uuid.UUID, str | None]]:
    """Each owned agent's owner account and that person's user id on `platform`, if any."""
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(AgentOwner.agent_id, AgentOwner.owner_account_id, PlatformPrincipal.external_id)
        .join(Account, Account.id == AgentOwner.owner_account_id)
        .outerjoin(
            PlatformPrincipal,
            (PlatformPrincipal.account_id == AgentOwner.owner_account_id)
            & (PlatformPrincipal.tenant_id == tenant_id)
            & (PlatformPrincipal.platform == platform),
        )
        .where(
            AgentOwner.tenant_id == tenant_id,
            AgentOwner.agent_id.in_(list(agent_ids)),
            Account.tenant_id == tenant_id,
        )
    )
    return {agent_id: (account_id, external_id) for agent_id, account_id, external_id in rows.all()}


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
