"""The library's skills as one caller reads them: names, versions and files, by scope.

Reads the local copy (`workspace_skills`) through `list_visible_skills`, so a scoped skill
shows only to a caller its scope checker names. Serving never touches the provider.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillFileRow
from daimon.core.workspace_skills import SkillScopes, list_visible_skills
from sqlalchemy.ext.asyncio import AsyncSession

MAIN_FILE = "SKILL.md"
SERVING_TOOLS: Final = frozenset({"list_library_skills", "read_library_skill"})
"""The tools that serve the library's skills (`daimon.adapters.mcp.tools.skill_serving`)."""


@dataclass(frozen=True)
class ServedSkill:
    name: str
    skill_id: str
    version: str
    files: tuple[WorkspaceSkillFileRow, ...]


async def list_served(
    session: AsyncSession,
    scopes: SkillScopes,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> list[ServedSkill]:
    """Every skill the caller may read, each with its latest version's files."""
    rows = await list_visible_skills(session, scopes, tenant_id=tenant_id, account_id=account_id)
    return [
        ServedSkill(
            row.name,
            row.skill_id,
            row.current_version,
            tuple(
                await store.list_files(
                    session, tenant_id=tenant_id, skill_id=row.skill_id, version=row.current_version
                )
            ),
        )
        for row in rows
    ]


async def read_served(
    session: AsyncSession,
    scopes: SkillScopes,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    name: str,
    path: str = MAIN_FILE,
) -> tuple[ServedSkill, bytes] | None:
    """One file of a skill the caller may read (by name); None when the skill is unknown,
    hidden from them or has no such file: the three look alike."""
    rows = await list_visible_skills(session, scopes, tenant_id=tenant_id, account_id=account_id)
    row = next((r for r in rows if name in (r.name, r.skill_id)), None)
    if row is None:
        return None
    body = await store.read_file(
        session, tenant_id=tenant_id, skill_id=row.skill_id, version=row.current_version, path=path
    )
    if body is None:
        return None
    files = await store.list_files(
        session, tenant_id=tenant_id, skill_id=row.skill_id, version=row.current_version
    )
    return ServedSkill(row.name, row.skill_id, row.current_version, tuple(files)), body
