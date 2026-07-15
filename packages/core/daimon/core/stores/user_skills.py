"""Free-function store for user_skills.

Tracks user-managed skill rows for content_hash dedup and orphan delete.
PK: (tenant_id, principal_id, agent_name, name).

No try/except — exceptions propagate. None from load_user_skill means 'not found',
NEVER 'something broke' (per architecture rule).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import UserSkill
from daimon.core.stores.domain import UserSkillRow
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def load_user_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str,
    name: str,
) -> UserSkillRow | None:
    orm = await session.get(UserSkill, (tenant_id, principal_id, agent_name, name))
    if orm is None:
        return None
    return UserSkillRow.model_validate(orm)


async def list_user_skills_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str,
) -> list[UserSkillRow]:
    stmt = (
        select(UserSkill)
        .where(
            UserSkill.tenant_id == tenant_id,
            UserSkill.principal_id == principal_id,
            UserSkill.agent_name == agent_name,
        )
        .order_by(UserSkill.name)
    )
    result = await session.execute(stmt)
    return [UserSkillRow.model_validate(row) for row in result.scalars()]


async def upsert_user_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str,
    name: str,
    source_repo_url: str,
    source_repo_branch: str,
    source_path: str,
    content_hash: str,
    anthropic_id: str | None,
    anthropic_latest_version: str | None,
) -> UserSkillRow:
    now = datetime.now(tz=UTC)
    stmt = (
        pg_insert(UserSkill)
        .values(
            tenant_id=tenant_id,
            principal_id=principal_id,
            agent_name=agent_name,
            name=name,
            source_repo_url=source_repo_url,
            source_repo_branch=source_repo_branch,
            source_path=source_path,
            content_hash=content_hash,
            anthropic_id=anthropic_id,
            anthropic_latest_version=anthropic_latest_version,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "principal_id", "agent_name", "name"],
            set_={
                "source_repo_url": source_repo_url,
                "source_repo_branch": source_repo_branch,
                "source_path": source_path,
                "content_hash": content_hash,
                "anthropic_id": anthropic_id,
                "anthropic_latest_version": anthropic_latest_version,
                "updated_at": now,
            },
        )
        .returning(UserSkill)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    return UserSkillRow.model_validate(orm)


async def list_user_skills_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> list[UserSkillRow]:
    """Return all user_skills rows for a tenant, across all principals and agents."""
    stmt = (
        select(UserSkill)
        .where(UserSkill.tenant_id == tenant_id)
        .order_by(UserSkill.agent_name, UserSkill.name)
    )
    result = await session.execute(stmt)
    return [UserSkillRow.model_validate(row) for row in result.scalars()]


async def delete_user_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str,
    name: str,
) -> None:
    stmt = delete(UserSkill).where(
        UserSkill.tenant_id == tenant_id,
        UserSkill.principal_id == principal_id,
        UserSkill.agent_name == agent_name,
        UserSkill.name == name,
    )
    await session.execute(stmt)


async def list_user_skill_repos_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> list[str]:
    """Return the distinct ``source_repo_url`` values tracked for an agent.

    Keyed on (tenant_id, agent_name) only — NOT principal_id. The user_skills
    ledger is per-agent (CR-01), and rows for one agent can carry different
    ledger principal_ids across history (pre-CR-01 rows, public-repo fallback).
    This is the de-facto "which repos has this agent ever synced" list — used to
    populate the remove-repo UI. Sorted for deterministic display.
    """
    stmt = (
        select(UserSkill.source_repo_url)
        .where(UserSkill.tenant_id == tenant_id, UserSkill.agent_name == agent_name)
        .distinct()
        .order_by(UserSkill.source_repo_url)
    )
    return list((await session.execute(stmt)).scalars())


async def list_user_skills_for_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    source_repo_url: str,
) -> list[UserSkillRow]:
    """Return all rows for one (agent, repo), across every principal_id.

    Principal-agnostic on purpose: a repo's rows may have been written under
    different ledger principal_ids over time; remove-repo must catch all of
    them, not just those under the current derived agent identity.
    """
    stmt = (
        select(UserSkill)
        .where(
            UserSkill.tenant_id == tenant_id,
            UserSkill.agent_name == agent_name,
            UserSkill.source_repo_url == source_repo_url,
        )
        .order_by(UserSkill.name)
    )
    result = await session.execute(stmt)
    return [UserSkillRow.model_validate(row) for row in result.scalars()]


async def delete_user_skills_for_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    source_repo_url: str,
) -> int:
    """Delete every row for one (agent, repo), across all principals. Returns rowcount.

    Counterpart to :func:`list_user_skills_for_repo`; same principal-agnostic
    WHERE so a remove-repo pass leaves no stranded rows.
    """
    result = await session.execute(
        delete(UserSkill).where(
            UserSkill.tenant_id == tenant_id,
            UserSkill.agent_name == agent_name,
            UserSkill.source_repo_url == source_repo_url,
        )
    )
    return cast(CursorResult[Any], result).rowcount


async def delete_user_skills_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Delete ALL user_skills rows for a principal, across every tenant_id. Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.

    WHERE is principal_id ONLY — deliberately NO tenant_id filter (D-06). Ghost
    rows stranded under stale tenant_ids by the 71-12 re-key must still be
    deleted; erasure must not depend on tenant bookkeeping being correct.
    """
    result = await session.execute(delete(UserSkill).where(UserSkill.principal_id == principal_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_user_skills_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Count user_skills rows that `delete_user_skills_for_principal` would delete. Read-only."""
    stmt = select(func.count()).select_from(UserSkill).where(UserSkill.principal_id == principal_id)
    return int((await session.execute(stmt)).scalar_one())


async def get_first_user_skill_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> UserSkillRow | None:
    """Return the first user_skill row for a principal, ordered by name, or None.

    Used for human-display "example" labels in /privacy cascade previews.
    Ordered by `name` (no created_at column; name is the stable orderer).
    """
    stmt = (
        select(UserSkill)
        .where(UserSkill.principal_id == principal_id)
        .order_by(UserSkill.name)
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else UserSkillRow.model_validate(orm)
