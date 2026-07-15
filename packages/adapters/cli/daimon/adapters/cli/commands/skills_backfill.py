"""daimon skills backfill-tenant-titles — one-off idempotent backfill command.

For each tenant whose agents pin legacy (un-prefixed or `{agent}/{name}`-shaped)
skills: re-create under the canonical tenant-prefixed title, re-pin agents, then
delete the legacy skill once no agent anywhere still references it (D-06/D-07/D-14).

Content sources:
- Seeded skills: rebuilt from the defaults/skills/<name>/ tree via build_skill_zip.
- Synced skills: re-created from user_skills row provenance (source_repo_url/branch/path).
- Unrecoverable rows: reported as MANUAL for operator decision.

Never calls skills.list (D-14 — broken pagination). Enumerates via agent pins.
Re-pins BEFORE deleting legacy (Pitfall 4). Idempotent: re-running after completion
is a no-op.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal

import httpx
import structlog
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_referenced_skill_ids
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.ma import delete_skill_and_versions
from daimon.core.skill_sync.bundler import extract_and_bundle
from daimon.core.skill_sync.fetcher import GitHubAuthError, GitHubTarballFetcher, GitHubUnreachable
from daimon.core.skill_zip import build_skill_zip
from daimon.core.stores.tenants import list_tenants_by_platform
from daimon.core.stores.user_skills import list_user_skills_for_tenant, upsert_user_skill
from pydantic import BaseModel
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults skills tree (seeded skill names)
# ---------------------------------------------------------------------------

_DEFAULTS_SKILLS_DIR = Path(__file__).parents[7] / "defaults" / "skills"


def _seeded_skill_names() -> frozenset[str]:
    """Return the set of skill names from the defaults/skills/ tree."""
    if not _DEFAULTS_SKILLS_DIR.is_dir():
        return frozenset()
    return frozenset(d.name for d in _DEFAULTS_SKILLS_DIR.iterdir() if d.is_dir())


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------

SkillClassification = Literal[
    "SKIP",
    "FOREIGN",
    "RECREATE_SEEDED",
    "RECREATE_SYNCED",
    "MANUAL",
]


def classify_skill(
    *,
    display_title: str,
    tenant_id: uuid.UUID,
    all_tenant_ids: frozenset[uuid.UUID],
    seeded_names: frozenset[str],
    user_skills_by_agent_name: dict[str, list[str]],
) -> SkillClassification:
    """Classify a legacy skill's display_title for a given pinning tenant.

    Parameters
    ----------
    display_title:
        The MA skill's display_title (retrieved by id, not skills.list).
    tenant_id:
        The tenant whose agent pins this skill.
    all_tenant_ids:
        All registered tenant ids (for FOREIGN detection).
    seeded_names:
        Bare names from defaults/skills/ tree.
    user_skills_by_agent_name:
        Mapping of agent_name -> list[skill_name] drawn from user_skills rows
        for this tenant. Used to detect RECREATE_SYNCED provenance.
    """
    # SKIP — already canonical for this tenant (idempotency hinge)
    if strip_tenant_prefix(tenant_id=tenant_id, display_title=display_title) is not None:
        return "SKIP"

    # FOREIGN — canonical for a DIFFERENT known tenant
    for other_tid in all_tenant_ids:
        if other_tid == tenant_id:
            continue
        if strip_tenant_prefix(tenant_id=other_tid, display_title=display_title) is not None:
            return "FOREIGN"

    # RECREATE_SEEDED — bare name matching a defaults/skills/<name> dir
    if display_title in seeded_names:
        return "RECREATE_SEEDED"

    # RECREATE_SYNCED — "{agent}/{name}" shape WITH a matching user_skills row
    if "/" in display_title:
        agent_part, name_part = display_title.split("/", 1)
        if name_part in user_skills_by_agent_name.get(agent_part, []):
            return "RECREATE_SYNCED"

    # MANUAL — anything else (bare non-seeded, synced with no provenance, etc.)
    return "MANUAL"


# ---------------------------------------------------------------------------
# Report row
# ---------------------------------------------------------------------------


class _BackfillRow(BaseModel):
    """Report row produced during the plan phase."""

    tenant_id: str
    agent_names: str  # comma-separated list of pinning agent names
    skill_id: str
    display_title: str
    classification: SkillClassification
    new_title: str  # empty for SKIP/FOREIGN/MANUAL
    new_skill_id: str  # empty before apply; filled in after create


# ---------------------------------------------------------------------------
# Command wiring
# ---------------------------------------------------------------------------

# Import the skills_app from the existing module (registered there)
from daimon.adapters.cli.commands.skills import skills_app  # noqa: E402


@skills_app.command("backfill-tenant-titles")
def skills_backfill_command(
    yes: Annotated[bool, YES_OPTION] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List what would be migrated without writing."),
    ] = False,
) -> None:
    """Re-create legacy skills under canonical tenant-prefixed titles.

    Enumerates every registered discord workspace, inspects each agent's custom
    skill pins, and re-creates any legacy (un-prefixed or {agent}/{name}-shaped)
    skills under their canonical tenant-scoped title. Re-pins each agent to the
    new skill, then deletes the legacy skill once no agent org-wide still pins it.

    Safe to re-run: already-prefixed skills are classified SKIP and untouched.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await skills_backfill(rt=rt, console=console, yes=yes, dry_run=dry_run)

    run_cli(_with_defaults(), console=console)


# ---------------------------------------------------------------------------
# Async implementation (plan + apply)
# ---------------------------------------------------------------------------


async def skills_backfill(
    *,
    rt: CliRuntime,
    console: Console,
    yes: bool,
    dry_run: bool,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Full backfill implementation — plan phase (always) + apply phase (unless --dry-run)."""
    # -----------------------------------------------------------------------
    # Plan phase: enumerate, classify, build report rows
    # -----------------------------------------------------------------------
    rows, _all_tenant_ids = await _plan_phase(rt.anthropic, rt.sessionmaker)

    actionable = [r for r in rows if r.classification not in ("SKIP", "FOREIGN")]
    recreate_rows = [r for r in rows if r.classification in ("RECREATE_SEEDED", "RECREATE_SYNCED")]

    if not actionable:
        console.print("[green]✓ No skills need backfilling.[/green]")
        return

    n_recreate = len(recreate_rows)
    n_manual = len([r for r in rows if r.classification == "MANUAL"])
    summary = f"{n_recreate} skill(s) to re-create" + (
        f" + {n_manual} MANUAL row(s) requiring operator action" if n_manual else ""
    )

    if dry_run:
        console.print(f"[yellow]dry-run:[/yellow] {summary}")
        emit_rows(
            console,
            actionable,
            columns=("tenant_id", "classification", "display_title", "new_title", "agent_names"),
            as_json=False,
        )
        return

    confirm_or_abort(console, summary, yes=yes)

    # -----------------------------------------------------------------------
    # Apply phase
    # -----------------------------------------------------------------------
    async def _run(http: httpx.AsyncClient) -> list[_BackfillRow]:
        return await _apply_phase(
            rt.anthropic,
            rt.sessionmaker,
            rows=rows,
            http_client=http,
            console=console,
        )

    if http_client is not None:
        applied_rows = await _run(http_client)
    else:
        async with httpx.AsyncClient(timeout=30.0) as http:
            applied_rows = await _run(http)

    # Print summary + all non-SKIP rows
    done_count = sum(
        1 for r in applied_rows if r.new_skill_id and "[DEFERRED]" not in r.new_skill_id
    )
    console.print(f"[green]✓ Re-created {done_count} skill(s).[/green]")
    visible = [r for r in applied_rows if r.classification != "SKIP"]
    emit_rows(
        console,
        visible,
        columns=("tenant_id", "classification", "display_title", "new_title", "new_skill_id"),
        as_json=False,
    )


# ---------------------------------------------------------------------------
# Plan phase helpers
# ---------------------------------------------------------------------------


async def _plan_phase(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[list[_BackfillRow], frozenset[uuid.UUID]]:
    """Enumerate all tenants, classify legacy pins, return report rows + all tenant ids."""
    tenants = await list_tenants_by_platform(sessionmaker, platform="discord")
    tenant_ids = sorted({t.id for t in tenants})
    all_tenant_ids = frozenset(tenant_ids)
    seeded_names = _seeded_skill_names()

    rows: list[_BackfillRow] = []

    for tenant_id in tenant_ids:
        # Load user_skills provenance for this tenant
        async with sessionmaker() as session, session.begin():
            us_rows = await list_user_skills_for_tenant(session, tenant_id=tenant_id)

        # Build lookup: agent_name -> [name, ...]
        user_skills_by_agent: dict[str, list[str]] = {}
        for us in us_rows:
            user_skills_by_agent.setdefault(us.agent_name, []).append(us.name)

        agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
        # Collect legacy skill ids pinned by this tenant's agents (D-14: by id, never skills.list)
        # skill_id -> list of agent names that pin it
        pinned: dict[str, list[str]] = {}
        for agent in agents:
            agent_name = agent.metadata.get("daimon_name", agent.name)
            for skill_pin in agent.skills:
                if skill_pin.type == "custom":
                    pinned.setdefault(skill_pin.skill_id, []).append(agent_name)

        for skill_id, agent_names in pinned.items():
            # Retrieve by id (D-14: never skills.list)
            skill = await client.beta.skills.retrieve(skill_id)
            title = skill.display_title or ""
            classification = classify_skill(
                display_title=title,
                tenant_id=tenant_id,
                all_tenant_ids=all_tenant_ids,
                seeded_names=seeded_names,
                user_skills_by_agent_name=user_skills_by_agent,
            )
            # Compute new_title for recreatable rows
            new_title = ""
            if classification == "RECREATE_SEEDED":
                new_title = tenant_scoped_display_title(tenant_id=tenant_id, name=title)
            elif classification == "RECREATE_SYNCED" and "/" in title:
                agent_part, name_part = title.split("/", 1)
                new_title = tenant_scoped_display_title(
                    tenant_id=tenant_id, name=name_part, agent_name=agent_part
                )

            rows.append(
                _BackfillRow(
                    tenant_id=str(tenant_id),
                    agent_names=", ".join(sorted(set(agent_names))),
                    skill_id=skill_id,
                    display_title=title,
                    classification=classification,
                    new_title=new_title,
                    new_skill_id="",
                )
            )

    return rows, all_tenant_ids


# ---------------------------------------------------------------------------
# Apply phase
# ---------------------------------------------------------------------------


async def _apply_phase(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    rows: list[_BackfillRow],
    http_client: httpx.AsyncClient,
    console: Console,
) -> list[_BackfillRow]:
    """Execute the backfill for all RECREATE_* rows.

    Ordering per Pitfall 4: re-pin BEFORE deleting any legacy skill.
    """
    applied: list[_BackfillRow] = []

    # Track: legacy_skill_id -> new_skill_id (for the post-repin delete pass)
    legacy_to_new: dict[str, str] = {}

    # Pass 1: re-create + re-pin all RECREATE_* rows
    for row in rows:
        if row.classification not in ("RECREATE_SEEDED", "RECREATE_SYNCED"):
            applied.append(row)
            continue

        tenant_id = uuid.UUID(row.tenant_id)
        new_skill_id = await _create_new_skill(
            client=client,
            sessionmaker=sessionmaker,
            http_client=http_client,
            console=console,
            row=row,
            tenant_id=tenant_id,
        )

        if new_skill_id is None:
            # Demoted to MANUAL — already printed a message; row already appended
            applied.append(
                _BackfillRow(
                    tenant_id=row.tenant_id,
                    agent_names=row.agent_names,
                    skill_id=row.skill_id,
                    display_title=row.display_title,
                    classification="MANUAL",
                    new_title=row.new_title,
                    new_skill_id="",
                )
            )
            continue

        legacy_to_new[row.skill_id] = new_skill_id

        # Re-pin: for each pinning agent, swap legacy skill id for new id
        agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
        for agent in agents:
            pinned_ids = [s.skill_id for s in agent.skills if s.type == "custom"]
            if row.skill_id not in pinned_ids:
                continue
            # Build updated skills list: replace old id with new id
            new_skills: list[object] = []
            for s in agent.skills:
                if s.type == "custom" and s.skill_id == row.skill_id:
                    new_skills.append({"type": "custom", "skill_id": new_skill_id})
                else:
                    new_skills.append(s.model_dump(mode="json"))
            # Retrieve fresh version before update (Pitfall 3 / rekey pattern)
            fresh = await client.beta.agents.retrieve(agent.id)
            try:
                await client.beta.agents.update(
                    fresh.id,
                    version=fresh.version,
                    skills=new_skills,  # type: ignore[arg-type]
                )
            except Exception as exc:  # noqa: BLE001 — per-agent error; run continues
                _log.warning(
                    "skills_backfill.repin_failed",
                    agent_id=agent.id,
                    skill_id=row.skill_id,
                    error=str(exc),
                )
                console.print(
                    f"[yellow]WARNING:[/yellow] re-pin failed for agent {agent.id}: {exc}"
                )

        applied.append(
            _BackfillRow(
                tenant_id=row.tenant_id,
                agent_names=row.agent_names,
                skill_id=row.skill_id,
                display_title=row.display_title,
                classification=row.classification,
                new_title=row.new_title,
                new_skill_id=new_skill_id,
            )
        )

    # Pass 2: delete legacy skills — only after ALL re-pins (Pitfall 4 / D-07)
    # Recompute the org-wide referenced set AFTER all re-pins
    if legacy_to_new:
        referenced_ids = await list_referenced_skill_ids(client)
        for legacy_id, new_id in legacy_to_new.items():
            if legacy_id not in referenced_ids:
                await delete_skill_and_versions(client, legacy_id)
                _log.info("skills_backfill.deleted_legacy", skill_id=legacy_id, new_id=new_id)
            else:
                _log.info(
                    "skills_backfill.deferred_delete",
                    skill_id=legacy_id,
                    reason="still referenced by at least one agent",
                )
                # Mark the row as DEFERRED in the summary
                applied = [
                    _BackfillRow(
                        tenant_id=r.tenant_id,
                        agent_names=r.agent_names,
                        skill_id=r.skill_id,
                        display_title=r.display_title,
                        classification=r.classification,
                        new_title=r.new_title,
                        new_skill_id=f"{r.new_skill_id}[DEFERRED]",
                    )
                    if r.skill_id == legacy_id
                    else r
                    for r in applied
                ]

    return applied


async def _create_new_skill(
    *,
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    console: Console,
    row: _BackfillRow,
    tenant_id: uuid.UUID,
) -> str | None:
    """Create a new skill under the canonical title. Returns the new skill_id or None on failure."""
    if row.classification == "RECREATE_SEEDED":
        skill_dir = _DEFAULTS_SKILLS_DIR / row.display_title
        pkg = build_skill_zip(skill_dir)
        try:
            with pkg.path.open("rb") as fh:
                created = await client.beta.skills.create(
                    display_title=row.new_title,
                    files=[("SKILL.zip", fh, "application/zip")],
                )
        finally:
            pkg.path.unlink(missing_ok=True)
        return created.id

    # RECREATE_SYNCED
    if "/" not in row.display_title:
        return None

    agent_part, name_part = row.display_title.split("/", 1)

    # Find the user_skills row for this (tenant, agent_name, skill_name)
    async with sessionmaker() as session, session.begin():
        us_rows = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    us_row = next(
        (r for r in us_rows if r.agent_name == agent_part and r.name == name_part),
        None,
    )
    if us_row is None:
        _log.warning(
            "skills_backfill.no_provenance_row",
            tenant_id=str(tenant_id),
            display_title=row.display_title,
        )
        console.print(
            f"[yellow]MANUAL:[/yellow] {row.display_title!r} (no user_skills provenance; skipping)"
        )
        return None

    # Re-fetch tarball + bundle
    fetcher = GitHubTarballFetcher(http_client)
    try:
        tarball = await fetcher.fetch_tarball(
            pat=None,
            url=us_row.source_repo_url,
            branch=us_row.source_repo_branch,
        )
    except (GitHubAuthError, GitHubUnreachable) as exc:
        _log.warning(
            "skills_backfill.fetch_failure",
            tenant_id=str(tenant_id),
            display_title=row.display_title,
            error=str(exc),
        )
        console.print(
            f"[yellow]MANUAL:[/yellow] {row.display_title!r} (fetch failed: {exc}; skipping)"
        )
        return None

    with tempfile.TemporaryDirectory() as tmp:
        extract_root = Path(tmp) / "extract"
        extract_root.mkdir()
        entries = await extract_and_bundle(
            tarball_bytes=tarball,
            extract_root=extract_root,
            repo_name=name_part,
            split=False,
        )
        if not entries or entries[0].prebuilt_zip is None:
            console.print(
                f"[yellow]MANUAL:[/yellow] {row.display_title!r} "
                f"(bundle produced no entries; skipping)"
            )
            return None

        zip_bytes = entries[0].prebuilt_zip
        created = await client.beta.skills.create(
            display_title=row.new_title,
            files=[("SKILL.zip", zip_bytes, "application/zip")],
        )
        new_skill_id = created.id

    # Update user_skills.anthropic_id to new MA id (plain data write — no migration)
    async with sessionmaker() as session, session.begin():
        await upsert_user_skill(
            session,
            tenant_id=us_row.tenant_id,
            principal_id=us_row.principal_id,
            agent_name=us_row.agent_name,
            name=us_row.name,
            source_repo_url=us_row.source_repo_url,
            source_repo_branch=us_row.source_repo_branch,
            source_path=us_row.source_path,
            content_hash=us_row.content_hash,
            anthropic_id=new_skill_id,
            anthropic_latest_version=us_row.anthropic_latest_version,
        )

    return new_skill_id
