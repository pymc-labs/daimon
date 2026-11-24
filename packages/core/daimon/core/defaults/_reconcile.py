"""Shared reconcile spine for both apply_defaults and reconcile_tenant_defaults.

`_reconcile_core` is the single source of the load-tree / validate-refs /
preflight / skill / env / agent / sweep sequence. Both public orchestrators
are thin delegating wrappers.

This module imports neither `apply` nor `provisioning` (Landmine L4).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.defaults.backfill_guidance import backfill_credential_guidance
from daimon.core.defaults.loader import (
    load_agent_specs,
    load_environment_specs,
    load_skill_paths,
    load_skill_spec,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_strict
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.owned import reconcile_owned_environment
from daimon.core.defaults.preflight import check_models_accepted
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.reconcile_environments import reconcile_environment
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.rename_agents import (
    AgentRenameAmbiguous,
    AgentRenameBlocked,
    SeededRename,
    move_seeded_rename,
    pending_renames,
    plan_seeded_rename,
    rename_seeded_agent,
    write_seeded_rename,
)
from daimon.core.defaults.report import Action, ApplyReport, ResourceKind, ResourceOutcome
from daimon.core.defaults.sweep import (
    sweep_removed_agents,
    sweep_removed_environments,
    sweep_removed_skills,
)
from daimon.core.errors import DaimonError, DefaultsError
from daimon.core.features.deployment import in_workspace
from daimon.core.features.registry import FEATURES, WORKSPACE_SKILLS
from daimon.core.model_presets import model_choice_id
from daimon.core.specs import AgentSpec, EnvironmentSpec
from daimon.core.stores.agent_rename import list_tenants_with_selfhosted_agent
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
        from daimon.core.channel_backend import backend_refusal_causes

        _log.warning(
            "defaults.sweep_failed", kind=kind, error=str(err), causes=backend_refusal_causes(err)
        )
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


def _workspace_skills(
    skill_dirs: list[Path], agent_specs: list[AgentSpec], tenant_id: uuid.UUID
) -> tuple[list[Path], list[AgentSpec]]:
    """The tree less the skills whose feature does not exist in the workspace
    (`WORKSPACE_SKILLS`): never seeded there, nor on its agents."""
    absent = {
        name
        for name, key in WORKSPACE_SKILLS.items()
        if not in_workspace(FEATURES[key], None, tenant_id)
    }
    if not absent:
        return skill_dirs, agent_specs
    return [d for d in skill_dirs if d.name not in absent], [
        spec.model_copy(update={"skills": [r for r in spec.skills if r.skill_id not in absent]})
        for spec in agent_specs
    ]


def _check_skill_references(agent_specs: list[AgentSpec], skill_names_present: set[str]) -> None:
    """Raise `DefaultsError` for an agent that names a custom skill the tree lacks."""
    for spec in agent_specs:
        for ref in spec.skills:
            if ref.type == "custom" and ref.skill_id not in skill_names_present:
                raise DefaultsError(
                    f"agent {spec.name!r} references unknown skill {ref.skill_id!r}; "
                    "add defaults/skills/<name>/SKILL.md first."
                )


def reconcile_lock_key(tenant_id: uuid.UUID) -> str:
    """The advisory lock key every reconcile and rename of this tenant takes."""
    return f"{_RECONCILE_LOCK_NAMESPACE}:{tenant_id}"


async def rename_selfhosted_defaults(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic | None,
    defaults_root: Path,
    *,
    dry_run: bool,
    skip_tenant_ids: Collection[uuid.UUID] = (),
    only_tenant_ids: Collection[uuid.UUID] | None = None,
) -> ApplyReport:
    """Run pending seeded renames for every tenant with a self-hosted record to move.

    Self-hosted deployments may have no Anthropic credentials, so this needs only
    Postgres; a `client` also lets each tenant's managed agents move with it.
    The tree is checked first, so a bad one fails before any write. Run it after
    `apply_defaults` has passed its pre-flight, and pass the tenants that apply
    renamed itself as `skip_tenant_ids` so none is reported twice;
    `only_tenant_ids` limits the pass to those tenants.
    """
    report = ApplyReport()
    agents_root = defaults_root / "agents"
    specs = load_agent_specs(agents_root) if agents_root.exists() else []
    skill_names = {load_skill_spec(d)[0].name for d in load_skill_paths(defaults_root / "skills")}
    _check_skill_references(specs, skill_names)
    for old, new in pending_renames({spec.name for spec in specs}):
        async with session_factory() as session:
            tenant_ids = await list_tenants_with_selfhosted_agent(session, name=old)
        for tenant_id in tenant_ids:
            if tenant_id in skip_tenant_ids or (
                only_tenant_ids is not None and tenant_id not in only_tenant_ids
            ):
                continue
            with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
                await _run_isolated(
                    report,
                    lambda tenant_id=tenant_id, old=old, new=new: rename_seeded_agent(
                        client,
                        session_factory,
                        tenant_id=tenant_id,
                        old=old,
                        new=new,
                        lock_key=reconcile_lock_key(tenant_id),
                        dry_run=dry_run,
                    ),
                    kind="agent",
                    name=new,
                )
    return report


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
    """Run one step, recording an error as its outcome; None adds nothing.

    Only a rename that must wait on the workspace is deferred; any other error fails.
    """
    try:
        outcome = await fn()
    except (APIError, DaimonError) as err:
        from daimon.core.channel_backend import backend_refusal_causes

        _log.warning(
            "defaults.reconcile_failed",
            kind=kind,
            name=name,
            error=str(err),
            causes=backend_refusal_causes(err),
        )
        deferred = isinstance(err, AgentRenameBlocked | AgentRenameAmbiguous)
        action = Action.DEFERRED if deferred else Action.FAILED
        outcome = ResourceOutcome(kind=kind, name=name, action=action, error=str(err))
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
    skill_dirs, agent_specs = _workspace_skills(skill_dirs, agent_specs, tenant_id)
    skill_names_present = {load_skill_spec(d)[0].name for d in skill_dirs}

    # Validate agent->skill references before any MA write.
    _check_skill_references(agent_specs, skill_names_present)

    lock_key = reconcile_lock_key(tenant_id)
    renames = pending_renames({s.name for s in agent_specs})
    if not anthropic_configured(client):
        # Records only: the rename moves them, then the owned pass finds them.
        for old, new in renames:
            await _run_isolated(
                report,
                lambda old=old, new=new: rename_seeded_agent(
                    None,
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
        return await _reconcile_owned(
            client,
            session_factory,
            agent_specs,
            env_specs,
            report=report,
            tenant_id=tenant_id,
            account_id=account_id,
            dry_run=dry_run,
            public_url=public_url,
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

    async def _halted(step: Callable[[], Awaitable[None]]) -> bool:
        """Run one rename step; True when it could not run (reported deferred or failed)."""
        outcome = await _run_isolated(report, step, kind="agent", name=renames[0][1])
        return outcome is not None

    async def _plan(
        session: AsyncSession,
    ) -> tuple[list[SeededRename], list[BetaManagedAgentsAgent] | None] | None:
        """The due renames and the listing they were planned from, which the agent
        pass reuses when none is due; None when it could not run."""
        if not renames:
            return [], None
        due: list[SeededRename] = []
        listed: list[BetaManagedAgentsAgent] = []

        async def _step() -> None:
            listed.extend(await list_agents_by_tenant(client, tenant_id=tenant_id))
            for old, new in renames:
                rename = await plan_seeded_rename(
                    listed, session, tenant_id=tenant_id, old=old, new=new
                )
                if rename is not None:
                    due.append(rename)

        return None if await _halted(_step) else (due, listed)

    async def _run_passes(
        skill_session: AsyncSession | None,
        *,
        skip_agents: bool,
        agents: list[BetaManagedAgentsAgent] | None,
    ) -> ApplyReport:
        # Skill pass.
        skills_view = None
        if skill_dirs:
            try:
                skills_view = await list_skills_strict(client)
            except (APIError, DaimonError) as err:
                from daimon.core.channel_backend import backend_refusal_causes

                _log.warning(
                    "defaults.skills_list_failed",
                    error=str(err),
                    causes=backend_refusal_causes(err),
                )
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
                    agents=agents,
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

    # Seeded agents are renamed in place before the agent pass and sweep: the
    # sweep would archive the old name and the pass would create the new one.
    # While a rename is deferred, failed or (dry run) pending, both are skipped.
    if dry_run:
        # Verification is read-only and must not acquire a DB lock transaction.
        async with session_factory() as read_session:
            planned = await _plan(read_session)
        for rename in planned[0] if planned else ():
            report.add(rename.outcome)
        return await _run_passes(
            None,
            skip_agents=planned is None or bool(planned[0]),
            agents=planned[1] if planned else None,
        )

    # Advisory lock covers every provider list/write through the final sweep.
    # The transaction context releases it on success, exception, or cancellation.
    # Share this session with the skill pass to avoid nested pool checkout while
    # holding a lock connection (some test and operator pools have one connection).
    async def _lock(session: AsyncSession) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key}
        )

    async def _finish(
        lock_session: AsyncSession,
        *,
        skip_agents: bool,
        agents: list[BetaManagedAgentsAgent] | None,
    ) -> ApplyReport:
        result = await _run_passes(lock_session, skip_agents=skip_agents, agents=agents)
        await prune_seeded_skills(
            lock_session, tenant_id=tenant_id, present_names=skill_names_present
        )
        return result

    async with session_factory() as lock_session, lock_session.begin():
        await _lock(lock_session)
        planned = await _plan(lock_session)
        due = planned[0] if planned else []
        if due:

            async def _write() -> None:
                async with lock_session.begin_nested():
                    for rename in due:
                        await write_seeded_rename(lock_session, rename, tenant_id=tenant_id)

            if await _halted(_write):
                due, planned = [], None
        if not due:
            return await _finish(
                lock_session, skip_agents=planned is None, agents=planned[1] if planned else None
            )

    # The rows committed first: a provider failure below leaves the agents under
    # their old names, so the next apply plans the same renames again.
    async def _move() -> None:
        for rename in due:
            await move_seeded_rename(client, session_factory, rename, tenant_id=tenant_id)

    moved = not await _halted(_move)
    if moved:
        for rename in due:
            report.add(rename.outcome)
    async with session_factory() as lock_session, lock_session.begin():
        await _lock(lock_session)
        return await _finish(lock_session, skip_agents=not moved, agents=None)


async def _reconcile_owned(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    agent_specs: list[AgentSpec],
    env_specs: list[EnvironmentSpec],
    *,
    report: ApplyReport,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    dry_run: bool,
    public_url: str | None,
) -> ApplyReport:
    """No Anthropic key: seed the environments and agents as Daimon records.

    No preflight, MA skill pass or sweep: those are Managed Agents resources.
    Skills reach these agents through the neutral skill host at admission.
    """
    for env in env_specs:
        await _run_per_resource(
            report,
            lambda env=env: reconcile_owned_environment(
                session_factory, env, tenant_id=tenant_id, dry_run=dry_run, account_id=account_id
            ),
            kind="environment",
            name=env.name,
        )
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
                sessions=session_factory,
            ),
            kind="agent",
            name=spec.name,
        )
    return report
