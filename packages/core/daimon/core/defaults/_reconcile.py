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
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.backfill_guidance import backfill_credential_guidance
from daimon.core.defaults.loader import (
    load_agent_specs,
    load_environment_specs,
    load_skill_paths,
    load_skill_spec,
)
from daimon.core.defaults.ma_index import list_skills_strict
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.preflight import check_models_accepted
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.reconcile_environments import reconcile_environment
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.rename_agents import pending_renames, rename_seeded_agent
from daimon.core.defaults.report import Action, ApplyReport, ResourceKind, ResourceOutcome
from daimon.core.defaults.sweep import (
    sweep_removed_agents,
    sweep_removed_environments,
    sweep_removed_skills,
)
from daimon.core.errors import DaimonError, DefaultsError
from daimon.core.model_presets import model_choice_id
from daimon.core.stores.channel_skills import list_channel_skill_ids
from daimon.core.stores.seeded_skills import prune_seeded_skills
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_RECONCILE_LOCK_NAMESPACE = "defaults_reconcile"

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
    skills_view: list[SkillListResponse] | None = None,
    channel_skill_ids: set[str] | None = None,
    session: AsyncSession | None = None,
) -> None:
    try:
        kwargs: dict[str, object] = {}
        if kind == "skill":
            if channel_skill_ids is None:
                # Without the channel rules the sweep could delete a skill one
                # still adds, failing every turn in that channel. Skip it.
                raise DefaultsError("channel skills could not be read; skill sweep skipped")
            kwargs["channel_skill_ids"] = channel_skill_ids
            kwargs["session"] = session
            if skills_view is not None:
                kwargs["skills_view"] = skills_view
        for outcome in await sweep_fn(
            client, present_names=present_names, tenant_id=tenant_id, dry_run=dry_run, **kwargs
        ):
            report.add(outcome)
    except (APIError, DaimonError) as err:
        _log.warning("defaults.sweep_failed", kind=kind, error=str(err))
        report.add(ResourceOutcome(kind=kind, name="<sweep>", action=Action.FAILED, error=str(err)))


async def _channel_skill_ids(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession | None
) -> set[str] | None:
    """Every skill id a channel rule adds, or None when the read failed.

    Reads on the lock session when there is one (a second checkout could
    starve a one-connection pool), inside a savepoint so a failed read leaves
    that transaction usable for the rest of the apply; a dry run reads on its
    own short session.
    """
    try:
        if session is not None:
            async with session.begin_nested():
                return await list_channel_skill_ids(session)
        async with session_factory() as read_session:
            return await list_channel_skill_ids(read_session)
    except SQLAlchemyError as err:
        _log.warning("defaults.channel_skills_read_failed", error=str(err))
        return None


async def _run_per_resource(
    report: ApplyReport,
    fn: Callable[[], Awaitable[ResourceOutcome]],
    *,
    kind: ResourceKind,
    name: str,
) -> ResourceOutcome:
    outcome = await _run_isolated(report, fn, kind=kind, name=name)
    assert outcome is not None, "fn returns an outcome"
    return outcome


async def _run_isolated(
    report: ApplyReport,
    fn: Callable[[], Awaitable[ResourceOutcome | None]],
    *,
    kind: ResourceKind,
    name: str,
) -> ResourceOutcome | None:
    """Run one step, recording a failure as its outcome; None (nothing done) adds nothing."""
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
    except Exception as err:  # per-resource isolation boundary
        _log.exception("defaults.reconcile_unexpected", kind=kind, name=name)
        outcome = ResourceOutcome(
            kind=kind,
            name=name,
            action=Action.FAILED,
            error=f"internal error: {type(err).__name__}",
        )
    if outcome is not None:
        report.add(outcome)
    return outcome


async def _reconcile_core(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
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

    `session_factory` provides the per-tenant advisory lock and the skill pass
    session: MA has no carrier for skill content idempotence, so the fingerprint
    lives in `seeded_skills`.
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
        unique_models = {model_choice_id(spec.model) for spec in agent_specs}
        rejections = await check_models_accepted(client, unique_models)
        failed = {m: err for m, err in rejections.items() if err is not None}
        if failed:
            raise DefaultsError(
                f"pre-flight failed: MA rejected {len(failed)} agent model(s); "
                f"aborting before any writes. Rejections: {failed}"
            )

    # Rename seeded agents in place before the agent pass and sweep: the sweep
    # would archive the old name and the pass would create the new one. While a
    # rename is blocked, failed or (dry run) pending, both are skipped.
    lock_key = f"{_RECONCILE_LOCK_NAMESPACE}:{tenant_id}"
    skip_agents = False
    for old, new in pending_renames({s.name for s in agent_specs}):
        outcome = await _run_isolated(
            report,
            lambda old=old, new=new: rename_seeded_agent(
                client,
                session_factory,
                tenant_id=tenant_id,
                old=old,
                new=new,
                lock_key=lock_key,
                dry_run=dry_run,
            ),
            kind="agent",
            name=new,
        )
        if outcome is not None and (outcome.action is Action.FAILED or dry_run):
            skip_agents = True

    async def _run_passes(skill_session: AsyncSession | None) -> ApplyReport:
        # Skill pass.
        skills_view = None
        if skill_dirs:
            try:
                skills_view = await list_skills_strict(client)
            except (APIError, DaimonError) as err:
                _log.warning("defaults.skills_list_failed", error=str(err))
                for skill_dir in skill_dirs:
                    report.add(
                        ResourceOutcome(
                            kind="skill", name=skill_dir.name, action=Action.FAILED, error=str(err)
                        )
                    )
        for skill_dir in skill_dirs:
            if skills_view is None:
                break
            await _run_per_resource(
                report,
                lambda skill_dir=skill_dir: reconcile_skill(
                    client,
                    session_factory,
                    skill_dir,
                    tenant_id=tenant_id,
                    dry_run=dry_run,
                    db_session=skill_session,
                    skills_view=skills_view,
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
        seeded_agent_ids: set[str] = set()
        for spec in [] if skip_agents else agent_specs:
            outcome = await _run_per_resource(
                report,
                lambda spec=spec: reconcile_agent(
                    client,
                    spec,
                    tenant_id=tenant_id,
                    dry_run=dry_run,
                    account_id=account_id,
                    public_url=public_url,
                    skills_view=skills_view if dry_run else None,
                    sessions=session_factory,
                ),
                kind="agent",
                name=spec.name,
            )
            if outcome.anthropic_id is not None:
                seeded_agent_ids.add(outcome.anthropic_id)

        # The seeded pass owns its own fingerprints. User-created agents have no
        # spec to reconcile, so refresh their system guidance separately.
        if not dry_run and not skip_agents:
            await backfill_credential_guidance(
                client,
                tenant_id=tenant_id,
                seeded_agent_ids=seeded_agent_ids,
                seeded_agent_names={spec.name for spec in agent_specs},
                seeded_account_id=account_id,
                sessions=session_factory,
            )

        # Sweep in reverse order. Skills sweep compares canonical tenant-scoped
        # display_titles, so map the bare authoring names through the shared title
        # function (seeded shape: agent_name=None).
        present_agents = {s.name for s in agent_specs}
        present_envs = {s.name for s in env_specs}
        present_skills = {
            tenant_scoped_display_title(tenant_id=tenant_id, name=name)
            for name in skill_names_present
        }
        if not skip_agents:
            await _run_sweep(
                report, sweep_removed_agents, "agent", client, present_agents, tenant_id, dry_run
            )
        await _run_sweep(
            report,
            sweep_removed_environments,
            "environment",
            client,
            present_envs,
            tenant_id,
            dry_run,
        )
        await _run_sweep(
            report,
            sweep_removed_skills,
            "skill",
            client,
            present_skills,
            tenant_id,
            dry_run,
            skills_view=skills_view,
            channel_skill_ids=await _channel_skill_ids(session_factory, skill_session),
            session=skill_session,
        )
        return report

    if dry_run:
        # Verification is read-only and must not acquire a DB lock transaction.
        return await _run_passes(skill_session=None)

    # Advisory lock covers every provider list/write through the final sweep.
    # The transaction context releases it on success, exception, or cancellation.
    # Share this session with the skill pass to avoid nested pool checkout while
    # holding a lock connection (some test and operator pools have one connection).
    async with session_factory() as lock_session, lock_session.begin():
        await lock_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": lock_key},
        )
        report = await _run_passes(skill_session=lock_session)
        await prune_seeded_skills(
            lock_session, tenant_id=tenant_id, present_names=skill_names_present
        )
        return report
