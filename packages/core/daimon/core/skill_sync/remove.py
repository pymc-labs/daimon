"""Remove one skill repo's skills from an agent.

The inverse of the orchestrator's attach step. ``sync_agent_skills`` is always
called with a single repo and orphan-deletes only that repo's rows, so a repo
that is no longer synced leaves its ``user_skills`` rows (and MA skill resources)
stranded forever — they keep counting against the per-agent skill cap and re-appear
in every attach union. There is no persisted "intended repo set" per agent, so the
only way to undo a repo add is an explicit removal, which this module provides.

NAMED ERROR BOUNDARY: like the orchestrator, this module catches per-skill MA
failures (best-effort delete) and records them; the DB row deletion always runs so
our view of MA does not drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
    BetaManagedAgentsSkillParams,
)
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma import delete_skill_and_versions, update_agent_with_version_retry
from daimon.core.stores.user_skills import (
    delete_user_skills_for_repo,
    list_user_skills_for_repo,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass
class RemoveReport:
    removed: int = 0  # user_skills rows deleted
    detached: int = 0  # skills unbound from the MA agent (0 if agent absent/none attached)
    # (skill_name, reason) — MA skill delete failed; the DB row was still removed.
    ma_delete_failures: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _compute_skills_after_removal(
    agent_skills: list[BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill],
    remove_ids: set[str],
) -> list[BetaManagedAgentsSkillParams] | None:
    """Return agent.skills minus ``remove_ids``, or None if nothing would change.

    Pure function — no I/O. Must be computed from the freshly-retrieved agent
    inside the retry closure (mirrors ``_compute_skills_union_list``). Returns an
    empty list when the removal clears the last skill (detach-all is a real
    update, distinct from the no-op None).
    """
    remaining = [s for s in agent_skills if s.skill_id not in remove_ids]
    if len(remaining) == len(agent_skills):
        return None  # none of the removed skills were attached → no-op
    result: list[BetaManagedAgentsSkillParams] = []
    for s in sorted(remaining, key=lambda s: (s.type, s.skill_id)):
        if s.type == "custom":
            result.append({"type": "custom", "skill_id": s.skill_id})
        elif s.type == "anthropic":
            result.append({"type": "anthropic", "skill_id": s.skill_id})
    return result


async def remove_agent_skill_repo(
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    repo_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic_client: AsyncAnthropic,
) -> RemoveReport:
    """Remove every skill that came from ``repo_url`` for one agent.

    Steps, in order (inverse of attach):

    1. Detach the repo's skill ids from the MA agent (``agents.update`` with
       version-retry), so the agent never references a skill we are about to
       delete.
    2. Best-effort delete each MA skill resource (tolerates 404 — a skill may
       have been deleted out of band).
    3. Delete the ``user_skills`` rows.

    Principal-agnostic by design: a repo's rows may carry different ledger
    principal_ids over history, so matching is on (tenant, agent_name, repo_url).

    Returns a :class:`RemoveReport`. Raises only on unexpected (non-404) MA
    errors during detach; per-skill delete failures are recorded, not raised.
    """
    report = RemoveReport()

    async with sessionmaker() as session, session.begin():
        rows = await list_user_skills_for_repo(
            session, tenant_id=tenant_id, agent_name=agent_name, source_repo_url=repo_url
        )
    if not rows:
        return report

    remove_ids = {row.anthropic_id for row in rows if row.anthropic_id is not None}

    # 1. Detach from the MA agent (if present and anything is attached).
    if remove_ids:
        agent = await find_agent_by_daimon_tag(
            anthropic_client, tenant_id=tenant_id, name=agent_name
        )
        if agent is not None:

            async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
                remaining = _compute_skills_after_removal(fresh.skills, remove_ids)
                if remaining is None:
                    return fresh  # nothing of ours was attached
                return await anthropic_client.beta.agents.update(
                    fresh.id,
                    version=fresh.version,
                    skills=remaining,
                )

            before = len(agent.skills)
            updated = await update_agent_with_version_retry(anthropic_client, agent.id, _apply)
            report.detached = before - len(updated.skills)

    # 2. Best-effort delete the MA skill resources.
    for row in rows:
        if row.anthropic_id is None:
            continue
        try:
            await delete_skill_and_versions(anthropic_client, row.anthropic_id)
        except anthropic.APIStatusError as err:
            _log.warning(
                "skill_sync.remove_ma_delete_failed",
                name=row.name,
                anthropic_id=row.anthropic_id,
                error=str(err),
            )
            report.ma_delete_failures.append((row.name, str(err)))

    # 3. Delete the local rows.
    async with sessionmaker() as session, session.begin():
        report.removed = await delete_user_skills_for_repo(
            session, tenant_id=tenant_id, agent_name=agent_name, source_repo_url=repo_url
        )

    _log.info(
        "skill_sync.remove_repo_done",
        agent_name=agent_name,
        repo_url=repo_url,
        removed=report.removed,
        detached=report.detached,
        ma_delete_failures=len(report.ma_delete_failures),
    )
    return report
