"""Store for agent_drafts and agent_versions: web edits of an agent and their history.

Configs and changes are stored as the JSON the web contract uses (`AgentConfigDoc`,
`Change`); this store does not interpret them.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from daimon.core._models import AgentDraft, AgentVersion
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


class AgentDraftRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: str
    account_id: uuid.UUID
    base_version: int
    base_config: dict[str, Any]
    config: dict[str, Any]
    updated_at: datetime


class AgentVersionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: str
    version: int
    account_id: uuid.UUID | None
    note: str | None
    changes: list[dict[str, Any]]
    config: dict[str, Any]
    at: datetime


async def lock_agent_edits(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str) -> None:
    """Wait for, then hold until the transaction ends, the agent's edit lock."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"agent_edit:{tenant_id}:{agent_id}", 0)
            )
        )
    )


async def get_draft(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str, account_id: uuid.UUID
) -> AgentDraftRow | None:
    orm = await session.get(AgentDraft, (tenant_id, agent_id, account_id))
    return None if orm is None else AgentDraftRow.model_validate(orm)


async def list_drafts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[AgentDraftRow]:
    rows = await session.execute(
        select(AgentDraft).where(
            AgentDraft.tenant_id == tenant_id, AgentDraft.account_id == account_id
        )
    )
    return [AgentDraftRow.model_validate(orm) for orm in rows.scalars()]


async def save_draft(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    account_id: uuid.UUID,
    base_version: int,
    base_config: dict[str, Any],
    config: dict[str, Any],
) -> None:
    values = {
        "base_version": base_version,
        "base_config": base_config,
        "config": config,
        "updated_at": func.now(),
    }
    await session.execute(
        insert(AgentDraft)
        .values(tenant_id=tenant_id, agent_id=agent_id, account_id=account_id, **values)
        .on_conflict_do_update(constraint="pk_agent_drafts", set_=values)
    )


async def delete_draft(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str, account_id: uuid.UUID
) -> None:
    await session.execute(
        delete(AgentDraft).where(
            AgentDraft.tenant_id == tenant_id,
            AgentDraft.agent_id == agent_id,
            AgentDraft.account_id == account_id,
        )
    )


async def get_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str, version: int
) -> AgentVersionRow | None:
    orm = await session.get(AgentVersion, (tenant_id, agent_id, version))
    return None if orm is None else AgentVersionRow.model_validate(orm)


async def list_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str
) -> list[AgentVersionRow]:
    """Every recorded version, newest first."""
    rows = await session.execute(
        select(AgentVersion)
        .where(AgentVersion.tenant_id == tenant_id, AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())
    )
    return [AgentVersionRow.model_validate(orm) for orm in rows.scalars()]


async def latest_version_by_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_ids: Collection[str]
) -> dict[str, AgentVersionRow]:
    """Each agent's newest recorded version in one read; an agent with none is absent."""
    if not agent_ids:
        return {}
    rows = await session.scalars(
        select(AgentVersion)
        .where(AgentVersion.tenant_id == tenant_id, AgentVersion.agent_id.in_(list(agent_ids)))
        .distinct(AgentVersion.agent_id)
        .order_by(AgentVersion.agent_id, AgentVersion.version.desc())
    )
    return {orm.agent_id: AgentVersionRow.model_validate(orm) for orm in rows}


async def record_base(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    version: int,
    config: dict[str, Any],
    at: datetime,
) -> None:
    """Keep the config a draft begins from; an existing record of the version wins."""
    await session.execute(
        insert(AgentVersion)
        .values(tenant_id=tenant_id, agent_id=agent_id, version=version, config=config, at=at)
        .on_conflict_do_nothing(constraint="pk_agent_versions")
    )


async def record_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    version: int,
    account_id: uuid.UUID,
    note: str | None,
    changes: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """Record a version the web app wrote.

    A change to places or tool rules writes no provider version, so it lands on the
    version already recorded: its changes follow the earlier ones, and the note and
    config are the newest. A bare base kept for the version is simply filled in.
    """
    values: dict[str, Any] = {
        "account_id": account_id,
        "note": note,
        "changes": changes,
        "config": config,
        "at": func.now(),
    }
    insertion = insert(AgentVersion).values(
        tenant_id=tenant_id, agent_id=agent_id, version=version, **values
    )
    await session.execute(
        insertion.on_conflict_do_update(
            constraint="pk_agent_versions",
            set_={
                **values,
                "note": func.concat_ws(" / ", AgentVersion.note, insertion.excluded.note),
                "changes": AgentVersion.changes.concat(insertion.excluded.changes),
            },
        )
    )
