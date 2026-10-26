"""Recording and changing who owns an agent, with an audit row for each change.

The owner may edit the agent (`daimon.core.channel_backend.AgentBackendService`,
`daimon.core.operation_policy`) without being a server admin. An agent with
no owner is edited by server admins only. Callers authorize: a creator is
recorded by the tool that created the agent; only an admin assigns or clears, or
adds and removes co-owners. An agent has one primary owner (`agent_owner`) and any
number of co-owners (`agent_co_owners`); both own it in every check.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_owners, security_audit
from daimon.core.stores.agent_owners import ForeignOwnerError as ForeignOwnerError
from daimon.core.stores.identity import find_platform_principal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@asynccontextmanager
async def agent_name_lock(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, name: str
) -> AsyncIterator[None]:
    """Serialize creating agents of one name in a tenant, until the block ends.

    The name check and the create are separate provider calls; holding this
    across both means a second creator of the same name sees the first agent
    and is refused, rather than both creating or one rewriting the other.
    """
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"agent_name:{tenant_id}:{name}"},
        )
        yield


@asynccontextmanager
async def member_copies_lock(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> AsyncIterator[None]:
    """Serialize one person's own copies in a tenant, until the block ends.

    The cap check (how many they already own) and the create are separate calls;
    held across both, two copies at once can never both fit under the cap.
    """
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"member_copies:{tenant_id}:{account_id}"},
        )
        yield


async def record_agent_creator(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Make the creating person the owner, unless the agent already has one.

    An account outside the tenant is never recorded: the agent, already
    created, stays admin-only (fails closed) and the miss is logged.
    """
    try:
        async with sessionmaker.begin() as session:
            recorded = await agent_owners.record_creator(
                session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
            if recorded:
                await _audit(session, tenant_id, agent_id, account_id, "create")
    except LookupError:
        _log.warning("agent_owner.creator_not_in_tenant", tenant_id=str(tenant_id))
        return False
    return recorded


async def assign_agent_owner(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    by_account_id: uuid.UUID | None,
) -> None:
    """An admin's assignment. Raises LookupError for an account outside the tenant."""
    async with sessionmaker.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            owner_account_id=owner_account_id,
            source="admin_assigned",
        )
        await _audit(session, tenant_id, agent_id, by_account_id, "assign")


async def clear_agent_owner(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    by_account_id: uuid.UUID | None,
) -> bool:
    async with sessionmaker.begin() as session:
        cleared = await agent_owners.clear_owner(session, tenant_id=tenant_id, agent_id=agent_id)
        if cleared:
            await _audit(session, tenant_id, agent_id, by_account_id, "clear")
    return cleared


async def add_agent_co_owner(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    by_account_id: uuid.UUID | None,
) -> bool:
    """An admin's addition of a co-owner; false when the person already owns the agent.

    Raises LookupError for an account outside the tenant and `ForeignOwnerError`
    for a person from another organisation.
    """
    async with sessionmaker.begin() as session:
        added = await agent_owners.add_co_owner(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            added_by_account_id=by_account_id,
        )
        if added:
            await _audit(
                session, tenant_id, agent_id, by_account_id, "add_co_owner", subject=account_id
            )
    return added


async def remove_agent_co_owner(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    by_account_id: uuid.UUID | None,
) -> bool:
    async with sessionmaker.begin() as session:
        removed = await agent_owners.remove_co_owner(
            session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
        )
        if removed:
            await _audit(
                session, tenant_id, agent_id, by_account_id, "remove_co_owner", subject=account_id
            )
    return removed


async def agent_co_owners(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[uuid.UUID]:
    async with sessionmaker() as session:
        return await agent_owners.co_owners_of(session, tenant_id, agent_id)


async def owns_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str | None,
    account_id: uuid.UUID | None = None,
    platform: str | None = None,
    platform_user_id: str | None = None,
) -> bool:
    """Whether the person (by account, or by platform user) owns the agent.

    The primary owner and every co-owner own it. A platform user with no principal
    yet owns nothing; this never creates one.
    A person from another organisation (`foreign_home_id`) is never an owner,
    whichever way they are named.
    """
    if ma_agent_id is None:
        return False
    if account_id is None and platform is not None and platform_user_id is not None:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
        )
        if principal is None or principal.foreign_home_id is not None:
            return False
        account_id = principal.account_id
    if account_id is None:
        return False
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    return await agent_owners.is_owner(session, tenant_id, agent_id, account_id)


async def agent_owner(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> uuid.UUID | None:
    async with sessionmaker() as session:
        return await agent_owners.owner_of(session, tenant_id, agent_id)


async def _audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    operation: str,
    *,
    subject: uuid.UUID | None = None,
) -> None:
    await security_audit.append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="agent_owner",
        operation=operation,
        outcome="allowed",
        reason="completed",
        scope=f"agent_owner:{agent_id}" + (f":{subject}" if subject is not None else ""),
    )
