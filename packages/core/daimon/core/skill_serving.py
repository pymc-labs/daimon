"""The library's skills as one caller reads them: names, versions and files.

Reads the local copy (`workspace_skills`) of the skills the caller may see (the adapter
picks them by scope and channel line). Serving never touches the provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillFileRow, WorkspaceSkillRow
from sqlalchemy.ext.asyncio import AsyncSession

MAIN_FILE = "SKILL.md"


@dataclass(frozen=True)
class ServedSkill:
    name: str
    skill_id: str
    version: str
    files: tuple[WorkspaceSkillFileRow, ...]


async def served(session: AsyncSession, row: WorkspaceSkillRow) -> ServedSkill:
    """`row`'s latest version with its files."""
    files = await store.list_files(
        session, tenant_id=row.tenant_id, skill_id=row.skill_id, version=row.current_version
    )
    return ServedSkill(row.name, row.skill_id, row.current_version, tuple(files))


async def read_served(
    session: AsyncSession, rows: Sequence[WorkspaceSkillRow], *, name: str, path: str = MAIN_FILE
) -> tuple[ServedSkill, bytes] | None:
    """One file of the skill among `rows` named `name`; None when there is no such skill or
    file: the two look alike."""
    row = next((r for r in rows if name in (r.name, r.skill_id)), None)
    if row is None:
        return None
    body = await store.read_file(
        session,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.current_version,
        path=path,
    )
    return None if body is None else (await served(session, row), body)
