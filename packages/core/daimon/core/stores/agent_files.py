"""Per-agent file store. Phase 15 (INFRA-02)."""

from __future__ import annotations

import uuid

from daimon.core._models import AgentFile
from daimon.core.errors import StoreError
from daimon.core.stores.domain import AgentFileRow
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def put_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
    content: str,
) -> AgentFileRow:
    """Upsert text content for (tenant_id, agent_id, key). Last-write-wins (D-02).

    Returns the post-write row. Uses `.returning(...)` + `scalar_one()` so the
    cursor — not the identity map — is the source of truth, mirroring
    `agent_repo_binding.set_binding`. This is the safe shape when the caller
    (or a downstream caller in the same session) reads this row again.
    """
    if key == "":
        raise StoreError("key must not be empty")

    stmt = (
        pg_insert(AgentFile)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key=key,
            content=content,
        )
        .on_conflict_do_update(
            constraint="pk_agent_files",
            set_={
                "content": content,
                "updated_at": func.now(),
            },
        )
        .returning(AgentFile)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return AgentFileRow.model_validate(orm)


async def get_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> AgentFileRow | None:
    """Return the row at (tenant_id, agent_id, key), or None if absent."""
    orm = await session.get(AgentFile, (tenant_id, agent_id, key))
    if orm is None:
        return None
    return AgentFileRow.model_validate(orm)


async def list_agent_files(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[AgentFileRow]:
    """Return all files for this (tenant, agent) ordered by key."""
    result = await session.execute(
        select(AgentFile)
        .where(
            AgentFile.tenant_id == tenant_id,
            AgentFile.agent_id == agent_id,
        )
        .order_by(AgentFile.key)
    )
    return [AgentFileRow.model_validate(o) for o in result.scalars().all()]


async def delete_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> None:
    """Delete the row at (tenant_id, agent_id, key). Idempotent — no raise if absent."""
    await session.execute(
        delete(AgentFile).where(
            AgentFile.tenant_id == tenant_id,
            AgentFile.agent_id == agent_id,
            AgentFile.key == key,
        )
    )
    await session.flush()
