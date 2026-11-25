"""Catch up the local record of the library with skills it has not recorded yet."""

from __future__ import annotations

import structlog
from daimon.core.agent_editing import AgentEditDeps, AgentEditError, SkillWriter
from daimon.core.agent_skill_files import download_files
from daimon.core.defaults.ma_index import list_skills_lenient
from daimon.core.skill_library import own_skills
from daimon.core.stores import workspace_skills as store
from daimon.core.workspace_skills import record_published

__all__ = ["record_library_skills"]

log = structlog.get_logger(__name__)


async def record_library_skills(deps: AgentEditDeps, *, writer: SkillWriter) -> int:
    """Record the latest version of each custom library skill the local record lacks.

    Returns how many were recorded. A skill whose files can't be read is skipped and tried
    again on the next pass.
    """
    rows, _truncated = await list_skills_lenient(deps.client)
    async with deps.session_factory() as session:
        held = {
            s.skill_id: s.current_version
            for s in await store.list_skills(session, tenant_id=writer.tenant_id)
        }
    recorded = 0
    for row, name in own_skills(writer, rows):
        version = row.latest_version
        if row.source != "custom" or not version or held.get(row.id) == version:
            continue
        try:
            files = await download_files(deps, actor=writer, skill_id=row.id, version=version)
        except AgentEditError:
            log.warning("workspace_skill.backfill_unreadable", skill_id=row.id, version=version)
            continue
        await record_published(
            deps.session_factory,
            tenant_id=writer.tenant_id,
            skill_id=row.id,
            name=name,
            version=version,
            files=files,
        )
        recorded += 1
    return recorded
