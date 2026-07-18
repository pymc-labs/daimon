"""Removed-YAML detection for each resource type.

Sweep reads the MA API directly (list-then-filter) with no DB reads.
The YAML validation in apply.py is the guard against unsafe sweeps.

Per main-design 'Delete vs archive on MA':
- Agents: MA archive only (no hard delete).
- Environments: MA archive only (no hard delete).
- Skills: hard-delete only.
"""

from __future__ import annotations

import uuid

import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import (
    list_agents_by_tenant,
    list_environments_by_tenant,
    list_referenced_skill_ids,
    list_skills_strict,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    strip_tenant_prefix,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.ma import delete_skill_and_versions

_log = structlog.get_logger(__name__)


def _is_defaults_managed(metadata: dict[str, str]) -> bool:
    """True when the resource was stamped by a defaults `reconcile_*` write.

    User forks (created via `daimon agents fork`, Discord `/agent-setup`, etc.)
    leave this marker unset so the sweep ignores them. Without this filter the
    sweep would archive every user-created resource on the next `defaults
    apply` (which runs on every scheduler boot).
    """
    return metadata.get(MA_METADATA_KEY_MANAGED) == "true"


async def sweep_removed_agents(
    client: AsyncAnthropic,
    *,
    present_names: set[str],
    tenant_id: uuid.UUID,
    dry_run: bool,
) -> list[ResourceOutcome]:
    all_agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
    outcomes: list[ResourceOutcome] = []
    for ag in all_agents:
        if not _is_defaults_managed(ag.metadata):
            continue
        name = ag.metadata.get(MA_METADATA_KEY_NAME, "")
        if name in present_names:
            continue
        if dry_run:
            outcomes.append(
                ResourceOutcome(kind="agent", name=name, action=Action.ARCHIVED, anthropic_id=ag.id)
            )
            continue
        await client.beta.agents.archive(ag.id)
        outcomes.append(
            ResourceOutcome(kind="agent", name=name, action=Action.ARCHIVED, anthropic_id=ag.id)
        )
    return outcomes


async def sweep_removed_environments(
    client: AsyncAnthropic,
    *,
    present_names: set[str],
    tenant_id: uuid.UUID,
    dry_run: bool,
) -> list[ResourceOutcome]:
    all_envs = await list_environments_by_tenant(client, tenant_id=tenant_id)
    outcomes: list[ResourceOutcome] = []
    for env in all_envs:
        if not _is_defaults_managed(env.metadata):
            continue
        name = env.metadata.get(MA_METADATA_KEY_NAME, "")
        if name in present_names:
            continue
        if dry_run:
            outcomes.append(
                ResourceOutcome(
                    kind="environment", name=name, action=Action.ARCHIVED, anthropic_id=env.id
                )
            )
            continue
        await client.beta.environments.archive(env.id)
        outcomes.append(
            ResourceOutcome(
                kind="environment", name=name, action=Action.ARCHIVED, anthropic_id=env.id
            )
        )
    return outcomes


async def sweep_removed_skills(
    client: AsyncAnthropic,
    *,
    present_names: set[str],
    tenant_id: uuid.UUID,
    dry_run: bool,
) -> list[ResourceOutcome]:
    """Delete seeded skills that belong to this tenant and are no longer in the defaults tree.

    present_names carries canonical (tenant-prefixed) display titles — the caller
    must map bare skill names through tenant_scoped_display_title before passing them
    here. The root cause of #129 was passing bare names, causing a mismatch against
    the prefixed titles on MA.

    Candidate filter (in order — all must pass):
      1. source == "custom"  (skip anthropic built-ins)
      2. display_title is not None
      3. strip_tenant_prefix returns non-None (only this tenant's own skills)
      4. "/" not in stripped body (seeded shape only — synced-shaped titles are NEVER
         sweep candidates; the sweep only manages seeded defaults)

    Skills passing all four filters are then spared if:
      - display_title is in present_names (still in the defaults tree), OR
      - skill id is in referenced_skill_ids (an agent still pins it)

    A full skills.list page raises SkillsListTruncatedError BEFORE any delete
    decision; _run_sweep's existing except (APIError, DaimonError) boundary converts
    it to ResourceOutcome(FAILED, "<sweep>") with zero deletions.
    """
    all_skills = await list_skills_strict(client)
    # Skills carry no daimon_managed marker (MA skills have no metadata field),
    # so — unlike the agent/env sweeps — we cannot tell a defaults-created skill
    # from a user-synced one. Spare any skill an agent still pins so the sweep
    # never deletes a live agent's skill out from under it (smoke-matrix #19).
    referenced_skill_ids = await list_referenced_skill_ids(client)
    outcomes: list[ResourceOutcome] = []
    for sk in all_skills:
        if sk.source != "custom":
            continue
        display_title = sk.display_title
        if display_title is None:
            continue
        stripped = strip_tenant_prefix(tenant_id=tenant_id, display_title=display_title)
        if stripped is None:
            # Not this tenant's skill — structurally impossible to be our candidate.
            continue
        if "/" in stripped:
            # Synced-shaped title ({t8}-{agent}/{name}) — never a seeded-sweep candidate.
            continue
        # This skill belongs to this tenant (seeded shape). Check spare conditions.
        if display_title in present_names:
            continue
        if sk.id in referenced_skill_ids:
            _log.info("defaults.sweep_skip_referenced_skill", name=display_title, skill_id=sk.id)
            continue
        if dry_run:
            outcomes.append(
                ResourceOutcome(
                    kind="skill", name=display_title, action=Action.DELETED, anthropic_id=sk.id
                )
            )
            continue
        await delete_skill_and_versions(client, sk.id)
        outcomes.append(
            ResourceOutcome(
                kind="skill", name=display_title, action=Action.DELETED, anthropic_id=sk.id
            )
        )
    return outcomes
