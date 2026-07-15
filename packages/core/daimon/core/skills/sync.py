"""Create-or-update discovered skills on MA.

Iterates a list of :class:`~daimon.core.skills.discover.DiscoveredSkill`
records (produced by :func:`~daimon.core.skills.discover.discover_skills`)
and pushes each to MA using the same two-state logic as
:func:`~daimon.core.defaults.reconcile_skills.reconcile_skill` — but as a
*batch* function with per-skill error isolation.

Per D-02 and D-05: this is independent from ``reconcile_skill``; they share
the same leaf helpers but do not call each other. A failure on one skill
records a ``FAILED`` outcome and the batch continues.

Matching is by CANONICAL tenant-prefixed display_title (``{t8}-{name}``),
produced via :func:`~daimon.core.defaults.metadata.tenant_scoped_display_title`
with ``agent_name=None`` (seeded/registry shape). This ensures stack-B skills
are tenant-isolated and distinct across guilds sharing one MA Workspace.
"""

from __future__ import annotations

import uuid

import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_skill_by_display_title
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.skill_zip import build_skill_zip
from daimon.core.skills.discover import DiscoveredSkill

_log = structlog.get_logger(__name__)


async def sync_skills(
    client: AsyncAnthropic,
    skills: list[DiscoveredSkill],
    *,
    tenant_id: uuid.UUID,
) -> list[ResourceOutcome]:
    """Create or update each skill in *skills* on MA.

    For each skill:

    - If the skill already exists on MA (matched by CANONICAL tenant-prefixed
      ``display_title``), a new version is uploaded via ``skills.versions.create``.
    - If not found, a new skill is created via ``skills.create`` with the canonical
      title.

    The canonical title is ``tenant_scoped_display_title(tenant_id, name, agent_name=None)``
    (seeded/registry shape: ``{t8}-{name}``). This ensures skills from different
    tenants syncing the same-named skill get distinct MA resources.

    Lookup uses ``on_truncation="raise"`` — a full-page response in a create context
    is unsafe (hidden duplicates); the per-skill ``except`` boundary surfaces
    :class:`~daimon.core.errors.SkillsListTruncatedError` as a ``FAILED`` outcome
    rather than silently creating a duplicate or missing the existing skill (D-13).

    Any exception raised while processing a single skill is caught; a
    ``FAILED`` outcome is recorded and the batch continues with the next skill.

    Args:
        client: Anthropic SDK client for MA API calls.
        skills: Discovered skills to sync.
        tenant_id: Owning tenant — determines the canonical title prefix.

    Returns:
        One :class:`~daimon.core.defaults.report.ResourceOutcome` per input
        skill, in input order.
    """
    outcomes: list[ResourceOutcome] = []
    for skill in skills:
        try:
            canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=skill.spec.name)
            ma_match = await find_skill_by_display_title(client, canonical, on_truncation="raise")
            pkg = build_skill_zip(skill.skill_dir, name=skill.spec.name)
            try:
                if ma_match is not None:
                    with pkg.path.open("rb") as fh:
                        await client.beta.skills.versions.create(
                            skill_id=ma_match.id,
                            files=[("SKILL.zip", fh, "application/zip")],
                        )
                    outcomes.append(
                        ResourceOutcome(
                            kind="skill",
                            name=skill.spec.name,
                            action=Action.UPDATED,
                            anthropic_id=ma_match.id,
                        )
                    )
                else:
                    with pkg.path.open("rb") as fh:
                        created = await client.beta.skills.create(
                            display_title=canonical,
                            files=[("SKILL.zip", fh, "application/zip")],
                        )
                    outcomes.append(
                        ResourceOutcome(
                            kind="skill",
                            name=skill.spec.name,
                            action=Action.CREATED,
                            anthropic_id=created.id,
                        )
                    )
            finally:
                pkg.path.unlink(missing_ok=True)
        except Exception as err:
            _log.warning("sync.skill_failed", name=skill.spec.name, error=str(err))
            outcomes.append(
                ResourceOutcome(
                    kind="skill",
                    name=skill.spec.name,
                    action=Action.FAILED,
                    error=str(err),
                )
            )
    return outcomes
