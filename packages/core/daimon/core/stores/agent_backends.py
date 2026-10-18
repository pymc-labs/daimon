"""Persistence for backend preferences; callers own the policy fence/transaction."""

from __future__ import annotations

import uuid

from daimon.core._models import (
    Account,
    AgentBackendRevisionRecord,
    Tenant,
)
from daimon.core.stores import read_persisted_columns
from daimon.core.stores.domain import AgentBackendRevision
from mux.contracts.ids import Provider
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def actor_exists(session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID) -> bool:
    tenant = await session.scalar(
        select(Tenant.id)
        .where(Tenant.id == tenant_id, Tenant.archived_at.is_(None))
        .with_for_update(read=True, key_share=True)
    )
    if tenant is None:
        return False
    return (
        await session.scalar(
            select(Account.id)
            .where(
                Account.id == account_id,
                Account.tenant_id == tenant_id,
            )
            .with_for_update(read=True, key_share=True)
        )
        is not None
    )


async def current(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    local: int | None = None,
    persisted: bool = False,
) -> AgentBackendRevision | None:
    """Use persisted columns for authority; ordinary callers retain ORM semantics."""
    query = select(AgentBackendRevisionRecord).where(
        AgentBackendRevisionRecord.tenant_id == tenant_id,
        AgentBackendRevisionRecord.agent_id == agent_id,
    )
    if local is not None:
        query = query.where(AgentBackendRevisionRecord.local == local)
    query = query.order_by(AgentBackendRevisionRecord.local.desc()).limit(1)
    if persisted:
        row = (await read_persisted_columns(session, query)).mappings().one_or_none()
    else:
        row = await session.scalar(query)
    return AgentBackendRevision.model_validate(row) if row is not None else None


async def put_revision(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    local: int,
    backend: Provider | None,
    digest: str,
    previous_local: int | None,
    agent_name: str,
    preview_digest: str,
    fleet_grant: JsonValue | None = None,
    drop_consent: JsonValue | None = None,
) -> AgentBackendRevision:
    row = AgentBackendRevisionRecord(
        tenant_id=tenant_id,
        agent_id=agent_id,
        local=local,
        backend=backend,
        digest=digest,
        previous_local=previous_local,
        agent_name=agent_name,
        preview_digest=preview_digest,
        fleet_grant=fleet_grant,
        drop_consent=drop_consent,
    )
    session.add(row)
    await session.flush()
    return AgentBackendRevision.model_validate(row)
