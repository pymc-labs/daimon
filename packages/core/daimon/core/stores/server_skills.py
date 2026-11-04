"""Store for skills mirrored from a skills server, their deployments, and agents' follow choices."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from daimon.core._models import AgentSkillFollow, ServerSkill, ServerSkillDeployment
from daimon.core.stores.domain import (
    ServerSkillDeploymentRow,
    ServerSkillRow,
    ServerSkillStatus,
)
from pydantic import JsonValue
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def create_mirror(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    server: str,
    uri: str,
    name: str,
    skill_id: str,
    version_label: str | None,
    digest: str,
    manifest: Mapping[str, str],
    allowed_tools: JsonValue,
    added_by_person_id: uuid.UUID | None,
    now: datetime,
) -> ServerSkillRow:
    row = ServerSkill(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        server=server,
        uri=uri,
        name=name,
        skill_id=skill_id,
        version_label=version_label,
        digest=digest,
        manifest=dict(manifest),
        allowed_tools=allowed_tools,
        status="current",
        added_by_person_id=added_by_person_id,
        last_checked_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return ServerSkillRow.model_validate(row)


async def get_mirror(session: AsyncSession, mirror_id: uuid.UUID) -> ServerSkillRow | None:
    row = await session.get(ServerSkill, mirror_id, populate_existing=True)
    return None if row is None else ServerSkillRow.model_validate(row)


async def find_mirror(
    session: AsyncSession, *, tenant_id: uuid.UUID, server: str, uri: str
) -> ServerSkillRow | None:
    row = await session.scalar(
        select(ServerSkill).where(
            ServerSkill.tenant_id == tenant_id, ServerSkill.server == server, ServerSkill.uri == uri
        )
    )
    return None if row is None else ServerSkillRow.model_validate(row)


async def mirror_of_skill(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str
) -> ServerSkillRow | None:
    row = await session.scalar(
        select(ServerSkill).where(
            ServerSkill.tenant_id == tenant_id, ServerSkill.skill_id == skill_id
        )
    )
    return None if row is None else ServerSkillRow.model_validate(row)


async def list_mirrors(
    session: AsyncSession, *, tenant_id: uuid.UUID | None = None
) -> list[ServerSkillRow]:
    """One tenant's mirrors, or every tenant's for the update worker."""
    stmt = select(ServerSkill).order_by(ServerSkill.tenant_id, ServerSkill.name)
    if tenant_id is not None:
        stmt = stmt.where(ServerSkill.tenant_id == tenant_id)
    return [ServerSkillRow.model_validate(row) for row in await session.scalars(stmt)]


async def record_check(
    session: AsyncSession,
    mirror_id: uuid.UUID,
    *,
    status: ServerSkillStatus,
    error: str | None,
    now: datetime,
) -> None:
    """When the server was last asked, and what that left the mirror as."""
    await session.execute(
        update(ServerSkill)
        .where(ServerSkill.id == mirror_id)
        .values(status=status, last_error=error, last_checked_at=now, updated_at=now)
    )


async def accept_version(
    session: AsyncSession,
    mirror_id: uuid.UUID,
    *,
    digest: str,
    manifest: Mapping[str, str],
    version_label: str | None,
    allowed_tools: JsonValue,
    now: datetime,
) -> None:
    """Take an upstream version as current, dropping any update that awaited approval."""
    await session.execute(
        update(ServerSkill)
        .where(ServerSkill.id == mirror_id)
        .values(
            digest=digest,
            manifest=dict(manifest),
            version_label=version_label,
            allowed_tools=allowed_tools,
            status="current",
            pending_digest=None,
            pending_label=None,
            pending_changes=None,
            last_error=None,
            last_checked_at=now,
            updated_at=now,
        )
    )


async def hold_for_approval(
    session: AsyncSession,
    mirror_id: uuid.UUID,
    *,
    digest: str,
    version_label: str | None,
    changes: Mapping[str, list[str]],
    now: datetime,
) -> None:
    await session.execute(
        update(ServerSkill)
        .where(ServerSkill.id == mirror_id)
        .values(
            status="pending",
            pending_digest=digest,
            pending_label=version_label,
            pending_changes=dict(changes),
            last_error=None,
            last_checked_at=now,
            updated_at=now,
        )
    )


async def list_deployments(
    session: AsyncSession, mirror_id: uuid.UUID
) -> dict[str, ServerSkillDeploymentRow]:
    """Each provider's deployed version of the mirror, by provider."""
    rows = await session.scalars(
        select(ServerSkillDeployment).where(ServerSkillDeployment.mirror_id == mirror_id)
    )
    return {row.provider: ServerSkillDeploymentRow.model_validate(row) for row in rows}


async def put_deployment(
    session: AsyncSession,
    *,
    mirror_id: uuid.UUID,
    tenant_id: uuid.UUID,
    provider: str,
    digest: str,
    version: str,
    now: datetime,
) -> None:
    if version in ("", "latest"):
        raise ValueError("a deployment needs a concrete version")
    stmt = insert(ServerSkillDeployment).values(
        mirror_id=mirror_id,
        tenant_id=tenant_id,
        provider=provider,
        digest=digest,
        version=version,
        updated_at=now,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ServerSkillDeployment.mirror_id, ServerSkillDeployment.provider],
            set_={"digest": digest, "version": version, "updated_at": now},
        )
    )


async def follows_of_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str
) -> dict[str, bool]:
    """The agent's follow choices by skill id; a skill not named follows."""
    rows = await session.scalars(
        select(AgentSkillFollow).where(
            AgentSkillFollow.tenant_id == tenant_id, AgentSkillFollow.agent_id == agent_id
        )
    )
    return {row.skill_id: row.follow for row in rows}


async def pinned_agents(session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str) -> set[str]:
    """The agents that pin the skill rather than follow it."""
    rows = await session.scalars(
        select(AgentSkillFollow.agent_id).where(
            AgentSkillFollow.tenant_id == tenant_id,
            AgentSkillFollow.skill_id == skill_id,
            AgentSkillFollow.follow.is_(False),
        )
    )
    return set(rows)


async def set_follow(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    skill_id: str,
    follow: bool,
    now: datetime,
) -> None:
    stmt = insert(AgentSkillFollow).values(
        tenant_id=tenant_id, agent_id=agent_id, skill_id=skill_id, follow=follow, updated_at=now
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                AgentSkillFollow.tenant_id,
                AgentSkillFollow.agent_id,
                AgentSkillFollow.skill_id,
            ],
            set_={"follow": follow, "updated_at": now},
        )
    )


async def forget_mirror(session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str) -> None:
    """Drop the mirror of a library skill that was deleted, with its follow choices."""
    await session.execute(
        delete(ServerSkill).where(
            ServerSkill.tenant_id == tenant_id, ServerSkill.skill_id == skill_id
        )
    )
    await session.execute(
        delete(AgentSkillFollow).where(
            AgentSkillFollow.tenant_id == tenant_id, AgentSkillFollow.skill_id == skill_id
        )
    )
