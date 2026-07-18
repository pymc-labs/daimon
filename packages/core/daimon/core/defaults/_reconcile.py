"""Shared reconcile spine for both apply_defaults and reconcile_tenant_defaults.

`_reconcile_core` is the single source of the load-tree / validate-refs /
preflight / skill / env / agent / sweep sequence. Both public orchestrators
are thin delegating wrappers.

This module imports neither `apply` nor `provisioning` (Landmine L4).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core.defaults.loader import (
    load_agent_specs,
    load_environment_specs,
    load_skill_paths,
    load_skill_spec,
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.preflight import check_models_accepted
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.reconcile_environments import reconcile_environment
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.report import Action, ApplyReport, ResourceKind, ResourceOutcome
from daimon.core.defaults.sweep import (
    sweep_removed_agents,
    sweep_removed_environments,
    sweep_removed_skills,
)
from daimon.core.errors import DaimonError, DefaultsError

_log = structlog.get_logger(__name__)

__all__ = ["_reconcile_core", "_run_per_resource", "_run_sweep"]

_SweepFn = Callable[..., Awaitable[list["ResourceOutcome"]]]


async def _run_sweep(
    report: ApplyReport,
    sweep_fn: _SweepFn,
    kind: ResourceKind,
    client: AsyncAnthropic,
    present_names: set[str],
    tenant_id: uuid.UUID,
    dry_run: bool,
) -> None:
    try:
        for outcome in await sweep_fn(
            client, present_names=present_names, tenant_id=tenant_id, dry_run=dry_run
        ):
            report.add(outcome)
    except (APIError, DaimonError) as err:
        _log.warning("defaults.sweep_failed", kind=kind, error=str(err))
        report.add(ResourceOutcome(kind=kind, name="<sweep>", action=Action.FAILED, error=str(err)))


async def _run_per_resource(
    report: ApplyReport,
    fn: Callable[[], Awaitable[ResourceOutcome]],
    *,
    kind: ResourceKind,
    name: str,
) -> ResourceOutcome:
    try:
        outcome = await fn()
    except (APIError, DaimonError) as err:
        _log.warning(
            "defaults.reconcile_failed",
            kind=kind,
            name=name,
            error=str(err),
        )
        outcome = ResourceOutcome(
            kind=kind,
            name=name,
            action=Action.FAILED,
            error=str(err),
        )
    except Exception as err:  # noqa: BLE001 — per-resource isolation boundary
        _log.exception("defaults.reconcile_unexpected", kind=kind, name=name)
        outcome = ResourceOutcome(
            kind=kind,
            name=name,
            action=Action.FAILED,
            error=f"internal error: {type(err).__name__}",
        )
    report.add(outcome)
    return outcome


async def _reconcile_core(
    client: AsyncAnthropic,
    defaults_root: Path,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    dry_run: bool,
    run_preflight: bool,
    public_url: str | None,
) -> ApplyReport:
    """Shared reconcile spine: load-tree → validate-refs → preflight → passes → sweep.

    Called by both `apply_defaults` (account_id=None, dry_run/run_preflight from caller)
    and `reconcile_tenant_defaults` (account_id=_derive_account_uuid(tenant_id),
    dry_run=False, run_preflight=True).
    """
    report = ApplyReport()

    # Load the tree. Any DefaultsError here is pre-write.
    agents_root = defaults_root / "agents"
    envs_root = defaults_root / "environments"
    skills_root = defaults_root / "skills"
    agent_specs = load_agent_specs(agents_root) if agents_root.exists() else []
    env_specs = load_environment_specs(envs_root) if envs_root.exists() else []
    skill_dirs = load_skill_paths(skills_root)
    skill_names_present = {load_skill_spec(d)[0].name for d in skill_dirs}

    # Validate agent->skill references before any MA write.
    for spec in agent_specs:
        for ref in spec.skills:
            if ref.type == "custom" and ref.skill_id not in skill_names_present:
                raise DefaultsError(
                    f"agent {spec.name!r} references unknown skill {ref.skill_id!r}; "
                    "add defaults/skills/<name>/SKILL.md first."
                )

    # Pre-flight: verify MA accepts every unique agent model before any writes.
    # Catches MA whitelist drift. Skip in dry-run — dry-run must be 100% read-only.
    if run_preflight and not dry_run and agent_specs:
        unique_models = {spec.model for spec in agent_specs}
        rejections = await check_models_accepted(client, unique_models)
        failed = {m: err for m, err in rejections.items() if err is not None}
        if failed:
            raise DefaultsError(
                f"pre-flight failed: MA rejected {len(failed)} agent model(s); "
                f"aborting before any writes. Rejections: {failed}"
            )

    # Skill pass.
    for skill_dir in skill_dirs:
        await _run_per_resource(
            report,
            lambda skill_dir=skill_dir: reconcile_skill(
                client, skill_dir, tenant_id=tenant_id, dry_run=dry_run
            ),
            kind="skill",
            name=skill_dir.name,
        )

    # Environment pass.
    for spec in env_specs:
        await _run_per_resource(
            report,
            lambda spec=spec: reconcile_environment(
                client, spec, tenant_id=tenant_id, dry_run=dry_run
            ),
            kind="environment",
            name=spec.name,
        )

    # Agent pass — account_id threads the guild ownership axis.
    for spec in agent_specs:
        await _run_per_resource(
            report,
            lambda spec=spec: reconcile_agent(
                client,
                spec,
                tenant_id=tenant_id,
                dry_run=dry_run,
                account_id=account_id,
                public_url=public_url,
            ),
            kind="agent",
            name=spec.name,
        )

    # Sweep in reverse order. Skills sweep compares canonical tenant-scoped
    # display_titles, so map the bare authoring names through the shared title
    # function (seeded shape: agent_name=None).
    present_agents = {s.name for s in agent_specs}
    present_envs = {s.name for s in env_specs}
    present_skills = {
        tenant_scoped_display_title(tenant_id=tenant_id, name=name) for name in skill_names_present
    }
    await _run_sweep(
        report, sweep_removed_agents, "agent", client, present_agents, tenant_id, dry_run
    )
    await _run_sweep(
        report, sweep_removed_environments, "environment", client, present_envs, tenant_id, dry_run
    )
    await _run_sweep(
        report, sweep_removed_skills, "skill", client, present_skills, tenant_id, dry_run
    )

    return report
