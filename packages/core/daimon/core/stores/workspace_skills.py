"""Store for the library skills' local versions, files and scope."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping

from daimon.core._models import WorkspaceSkill, WorkspaceSkillFile, WorkspaceSkillVersion
from daimon.core.stores.domain import WorkspaceSkillFileRow, WorkspaceSkillRow
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    skill_id: str,
    name: str,
    version: str,
    digest: str,
    files: Mapping[str, bytes],
) -> None:
    """Keep a published version's files and make it the skill's latest.

    Recording a version again changes nothing, so a retry after a crash is safe. The scope
    of an existing skill stays as it was.
    """
    await session.execute(
        insert(WorkspaceSkill)
        .values(tenant_id=tenant_id, skill_id=skill_id, name=name, current_version=version)
        .on_conflict_do_update(
            index_elements=["tenant_id", "skill_id"],
            set_={"name": name, "current_version": version, "updated_at": func.now()},
        )
    )
    await session.execute(
        insert(WorkspaceSkillVersion)
        .values(tenant_id=tenant_id, skill_id=skill_id, version=version, digest=digest)
        .on_conflict_do_nothing()
    )
    await session.execute(
        insert(WorkspaceSkillFile)
        .values(
            [
                {
                    "tenant_id": tenant_id,
                    "skill_id": skill_id,
                    "version": version,
                    "path": path,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                    "content": body,
                }
                for path, body in files.items()
            ]
        )
        .on_conflict_do_nothing()
    )


async def get_skill(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str
) -> WorkspaceSkillRow | None:
    row = await session.get(WorkspaceSkill, (tenant_id, skill_id), populate_existing=True)
    return None if row is None else WorkspaceSkillRow.model_validate(row)


async def list_skills(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[WorkspaceSkillRow]:
    rows = await session.scalars(
        select(WorkspaceSkill)
        .where(WorkspaceSkill.tenant_id == tenant_id)
        .order_by(WorkspaceSkill.name)
    )
    return [WorkspaceSkillRow.model_validate(row) for row in rows]


async def list_files(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str, version: str
) -> list[WorkspaceSkillFileRow]:
    rows = await session.scalars(
        select(WorkspaceSkillFile)
        .where(
            WorkspaceSkillFile.tenant_id == tenant_id,
            WorkspaceSkillFile.skill_id == skill_id,
            WorkspaceSkillFile.version == version,
        )
        .order_by(WorkspaceSkillFile.path)
    )
    return [WorkspaceSkillFileRow.model_validate(row) for row in rows]


async def read_file(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str, version: str, path: str
) -> bytes | None:
    return await session.scalar(
        select(WorkspaceSkillFile.content).where(
            WorkspaceSkillFile.tenant_id == tenant_id,
            WorkspaceSkillFile.skill_id == skill_id,
            WorkspaceSkillFile.version == version,
            WorkspaceSkillFile.path == path,
        )
    )


async def read_files(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str, version: str
) -> dict[str, bytes]:
    rows = await session.execute(
        select(WorkspaceSkillFile.path, WorkspaceSkillFile.content).where(
            WorkspaceSkillFile.tenant_id == tenant_id,
            WorkspaceSkillFile.skill_id == skill_id,
            WorkspaceSkillFile.version == version,
        )
    )
    return {path: content for path, content in rows.tuples()}


async def set_scope(
    session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str, scope: str | None
) -> bool:
    """Set who may see the skill; False when the skill has no local record."""
    result = await session.execute(
        update(WorkspaceSkill)
        .where(WorkspaceSkill.tenant_id == tenant_id, WorkspaceSkill.skill_id == skill_id)
        .values(scope=scope, updated_at=func.now())
        .returning(WorkspaceSkill.skill_id)
    )
    return result.first() is not None


async def forget_skill(session: AsyncSession, *, tenant_id: uuid.UUID, skill_id: str) -> None:
    """Drop a deleted library skill's local record; its versions and files go with it."""
    await session.execute(
        delete(WorkspaceSkill).where(
            WorkspaceSkill.tenant_id == tenant_id, WorkspaceSkill.skill_id == skill_id
        )
    )
