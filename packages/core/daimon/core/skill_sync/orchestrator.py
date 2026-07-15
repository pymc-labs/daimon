"""Skill sync orchestrator (Phase 33).

NAMED ERROR BOUNDARY: per the architecture rule, this is the ONLY module in the
skill_sync package allowed to catch per-repo and per-skill exceptions. Fetcher,
bundler, and stores let exceptions propagate; the orchestrator records failures
in SyncReport and continues.

Concurrency: bounded by _UPLOAD_CONCURRENCY (=6). Per-skill timeout is
_PER_SKILL_TIMEOUT_S (=60.0). Both are module-level constants.

Display titles: produced exclusively by `tenant_scoped_display_title` from
`daimon.core.defaults.metadata` (D-01/D-03). Titles are tenant-prefixed
`{t8}-{agent_name}/{name}` so skills from two guilds syncing the same-named
skill produce two distinct MA skills with no cross-tenant collision.

Pagination: MA's skills.list never populates `next_page` at any page boundary
(live probe 2026-06-10, scripts/probes/managed_agents/list_pagination.py).
Full page = truncated org view. The recovery branch uses
`find_skill_by_display_title(on_truncation="raise")` from `daimon.core.defaults.
ma_index` — truncation raises `SkillsListTruncatedError`, which the `_upload_all`
boundary records in `report.failed_uploads` (D-13).

Recovery namespace check: before pushing a version onto a recovered skill, we
verify the recovered skill's display_title carries this tenant's prefix (D-07,
#138). If it does not, the push is refused with a `DefaultsError`.

Final step attaches uploaded skills (and any pre-existing user_skills rows)
onto the MA agent via `client.beta.agents.update(agent_id, skills=...)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
    BetaManagedAgentsSkillParams,
)
from cryptography.fernet import MultiFernet
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_skill_by_display_title,
)
from daimon.core.defaults.metadata import (
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.errors import DefaultsError
from daimon.core.github_credentials import get_pat
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync.bundler import SkillEntry, extract_and_bundle
from daimon.core.skill_sync.fetcher import (
    GitHubAuthError,
    GitHubTarballFetcher,
    GitHubUnreachable,
    RepoCollisionError,
    TarballTooLarge,
)
from daimon.core.skill_zip import canonical_zip_bytes
from daimon.core.specs import SkillRepo, merge_default_agent_toolset
from daimon.core.stores.user_skills import (
    delete_user_skill,
    list_user_skills_for_agent,
    load_user_skill,
    upsert_user_skill,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_UPLOAD_CONCURRENCY: int = 6
_PER_SKILL_TIMEOUT_S: float = 60.0


def _compute_skills_union_list(
    agent_skills: list[BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill],
    row_anthropic_ids: list[str],
) -> list[BetaManagedAgentsSkillParams] | None:
    """Compute the union of an agent's existing skills and the new row ids.

    Returns the sorted union list to pass to agents.update, or None if the union
    equals the existing set (no-op). Pure function — no I/O.

    `agent_skills` is the list from the freshly-retrieved agent (must be the fresh
    agent inside the retry closure, not an earlier read). `row_anthropic_ids` are
    the non-None anthropic_ids from user_skills DB rows (DB state; stays outside
    the closure).
    """
    existing_pairs: set[tuple[str, str]] = {(s.type, s.skill_id) for s in agent_skills}
    union_pairs: set[tuple[str, str]] = set(existing_pairs)
    for anthropic_id in row_anthropic_ids:
        union_pairs.add(("custom", anthropic_id))

    if union_pairs == existing_pairs:
        return None  # no-op

    result: list[BetaManagedAgentsSkillParams] = []
    for t, sid in sorted(union_pairs):
        if t == "custom":
            result.append({"type": "custom", "skill_id": sid})
        elif t == "anthropic":
            result.append({"type": "anthropic", "skill_id": sid})
        else:
            # Unknown skill type from MA — skip rather than send a value the
            # SDK's Literal won't accept. (Future-proofing only; MA today
            # returns "custom" | "anthropic".)
            continue
    return result


@dataclass
class SyncReport:
    synced: int = 0  # newly created skills on MA
    updated: int = 0  # new versions pushed to existing MA skills
    deleted: int = 0  # orphan-deleted from MA + local (filled by 07b)
    skipped_repos: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    failed_uploads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    # (agent_name, reason) — attach step refused by MA (e.g. per-agent skill cap).
    # Uploads succeeded; only the agents.update binding failed.
    attach_failures: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


class SyncRepoFailure(BaseModel):
    """Warning DTO for a repo or skill that failed during a sync run.

    Consumed by the MCP envelope (Plan 02) and Discord toast (Plan 03).

    ``repo_url`` carries the repo URL for fetch-phase failures and the skill
    name for upload-phase failures (the repo URL is not cleanly derivable from
    the skill name — Pitfall 4).
    """

    repo_url: str
    reason: str
    phase: Literal["fetch", "upload", "attach"] | None = None


def sync_report_failures(report: SyncReport) -> list[SyncRepoFailure]:
    """Translate a SyncReport's error fields into a flat list of SyncRepoFailure DTOs.

    Pure function — no I/O, safe to call from any adapter.

    Mapping rules:
    - ``skipped_repos`` tuples ``(repo_url, reason)`` → phase="fetch"
    - ``failed_uploads`` tuples ``(skill_name, reason)`` → phase="upload",
      ``repo_url=skill_name`` (repo URL is not derivable; see Pitfall 4)
    """
    failures: list[SyncRepoFailure] = []
    for repo_url, reason in report.skipped_repos:
        failures.append(SyncRepoFailure(repo_url=repo_url, reason=reason, phase="fetch"))
    for skill_name, reason in report.failed_uploads:
        failures.append(SyncRepoFailure(repo_url=skill_name, reason=reason, phase="upload"))
    for agent_name, reason in report.attach_failures:
        failures.append(SyncRepoFailure(repo_url=agent_name, reason=reason, phase="attach"))
    return failures


def _looks_like_duplicate_title(err: anthropic.APIStatusError) -> bool:
    """Heuristic: is this APIStatusError MA's duplicate-display_title rejection?

    Probed shape (scripts/probes/managed_agents/dup_display_title.py, 2026-05-09):
      - status_code: 400
      - message: "Skill cannot reuse an existing display_title: <title>"
      - body['error']['type']: 'invalid_request_error'

    Match the exact phrase. The earlier loose check ("display_title" in msg)
    matched false positives — e.g. `display_title must be at most 64
    characters long` — and steered length-rejection errors into the recovery
    branch where find_skill_by_display_title returned None (verified live
    2026-05-09, scripts/probes/managed_agents/sync_agent_skills_live.py).
    """
    if getattr(err, "status_code", None) != 400:
        return False
    msg = (err.message or "").lower()
    return "cannot reuse an existing display_title" in msg


def _looks_like_skill_cap(err: anthropic.APIStatusError) -> bool:
    """Heuristic: is this APIStatusError MA's per-agent skill-cap rejection?

    Observed shape (live 2026-06-16): status_code=400, message
    "Agent has invalid configuration: skills: 26 exceeds maximum of 20 for this
    organization". We match on the cap phrasing rather than a hardcoded number so
    the check tracks whatever the org limit is. The cap is enforced at
    agents.update, not at skills.create — uploads succeed; only the attach binding
    is rejected.
    """
    if getattr(err, "status_code", None) != 400:
        return False
    msg = (err.message or "").lower()
    return "skills:" in msg and "exceeds maximum of" in msg


@dataclass
class _PendingSkill:
    """Per-skill work item passed to _process_one."""

    name: str
    repo_url: str
    repo_branch: str
    repo_path: str
    skill_dir: Path
    prebuilt_zip: bytes | None


async def _process_one(
    *,
    pending: _PendingSkill,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_name: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic_client: AsyncAnthropic,
    report: SyncReport,
    report_lock: asyncio.Lock,
) -> None:
    """Build canonical zip, dedup, upload (create or version), upsert store row.

    Raises on unexpected errors — the caller's wait_for+except block catches.
    """
    # 1. Build deterministic zip bytes (in to_thread for split-mode CPU work).
    if pending.prebuilt_zip is not None:
        zip_bytes = pending.prebuilt_zip
    else:
        zip_bytes = await asyncio.to_thread(
            canonical_zip_bytes,
            pending.skill_dir,
            arcname_prefix=pending.name,
        )

    new_hash = hashlib.sha256(zip_bytes).hexdigest()
    display_title = tenant_scoped_display_title(
        tenant_id=tenant_id, name=pending.name, agent_name=agent_name
    )

    # 2. Dedup check.
    async with sessionmaker() as session, session.begin():
        existing = await load_user_skill(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            agent_name=agent_name,
            name=pending.name,
        )

    if (
        existing is not None
        and existing.content_hash == new_hash
        and existing.anthropic_id is not None
    ):
        # No change — short-circuit.
        _log.info("skill_sync.dedup_skip", name=pending.name, content_hash=new_hash)
        return

    # 3. MA upload.
    anthropic_id: str
    latest_version: str | None
    if existing is None or existing.anthropic_id is None:
        try:
            created = await anthropic_client.beta.skills.create(
                display_title=display_title,
                files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
            )
            anthropic_id = created.id
            latest_version = created.latest_version
            async with report_lock:
                report.synced += 1
        except anthropic.APIStatusError as err:
            if not _looks_like_duplicate_title(err):
                raise
            # Recovery: a previous sync crashed between MA-create and store
            # upsert (or two concurrent syncs raced). Look up the existing
            # skill by display_title and push a new version onto it.
            # on_truncation="raise" ensures a truncated view raises
            # SkillsListTruncatedError rather than silently hiding the duplicate
            # (D-13). The _upload_all except-Exception boundary records it in
            # report.failed_uploads.
            recovered = await find_skill_by_display_title(
                anthropic_client, display_title, on_truncation="raise"
            )
            if recovered is None:
                raise
            # D-07 / #138: Namespace check — refuse to push a version onto a
            # skill that doesn't belong to this tenant. Post-D-01 titles can't
            # collide cross-tenant, so this is defense in depth. A None
            # display_title (unprefixed MA skill) also fails the check.
            recovered_title: str = recovered.display_title or ""
            if strip_tenant_prefix(tenant_id=tenant_id, display_title=recovered_title) is None:
                raise DefaultsError(
                    f"duplicate-title collision resolved to a skill outside this tenant's "
                    f"namespace (tenant={str(tenant_id)[:8]}, recovered.display_title="
                    f"{recovered.display_title!r}); push refused (#138)"
                ) from err
            resp = await anthropic_client.beta.skills.versions.create(
                skill_id=recovered.id,
                files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
            )
            anthropic_id = recovered.id
            latest_version = resp.version
            async with report_lock:
                report.updated += 1
    else:
        # Version-create path.
        resp = await anthropic_client.beta.skills.versions.create(
            skill_id=existing.anthropic_id,
            files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
        )
        anthropic_id = existing.anthropic_id
        latest_version = resp.version
        async with report_lock:
            report.updated += 1

    # 4. Store upsert.
    async with sessionmaker() as session, session.begin():
        await upsert_user_skill(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            agent_name=agent_name,
            name=pending.name,
            source_repo_url=pending.repo_url,
            source_repo_branch=pending.repo_branch,
            source_path=pending.repo_path,
            content_hash=new_hash,
            anthropic_id=anthropic_id,
            anthropic_latest_version=latest_version,
        )


async def _upload_all(
    *,
    pending_skills: list[_PendingSkill],
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_name: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic_client: AsyncAnthropic,
    report: SyncReport,
    report_lock: asyncio.Lock,
) -> None:
    sem = asyncio.Semaphore(_UPLOAD_CONCURRENCY)

    async def _one(pending: _PendingSkill) -> None:
        async with sem:
            try:
                await asyncio.wait_for(
                    _process_one(
                        pending=pending,
                        principal_id=principal_id,
                        tenant_id=tenant_id,
                        agent_name=agent_name,
                        sessionmaker=sessionmaker,
                        anthropic_client=anthropic_client,
                        report=report,
                        report_lock=report_lock,
                    ),
                    timeout=_PER_SKILL_TIMEOUT_S,
                )
            except TimeoutError:
                async with report_lock:
                    report.failed_uploads.append(
                        (pending.name, f"timeout after {_PER_SKILL_TIMEOUT_S:.0f}s")
                    )
            except Exception as err:  # noqa: BLE001 — orchestrator IS the named boundary
                _log.warning("skill_sync.skill_failed", name=pending.name, error=str(err))
                async with report_lock:
                    report.failed_uploads.append((pending.name, str(err)))

    await asyncio.gather(*(_one(p) for p in pending_skills))


async def sync_agent_skills(
    *,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_name: str,
    repos: list[SkillRepo],
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    http_client: httpx.AsyncClient,
    anthropic_client: AsyncAnthropic,
    credential_override: str | None = None,
    max_tarball_bytes: int = 50 * 1024 * 1024,
) -> SyncReport:
    """Sync all skill_repos for one agent. Named error boundary.

    WR-01: ``credential_override`` lets a caller (the webhook resync) pass a
    pre-selected GitHub credential (App installation token > per-agent PAT > anon,
    per D-21) so this function does NOT independently re-resolve a per-agent PAT.
    When provided, it is the single authority for the fetch ``Authorization`` header
    and the internal ``get_pat`` resolution is skipped — this is what makes the
    App-installation-token tier actually win when both an installation and a
    per-agent PAT exist. When None (panel / CLI / MCP paths), the per-agent PAT is
    resolved here as before.

    ``max_tarball_bytes`` bounds the per-repo tarball download (RATE-03,
    D-13/D-11). Defaults to the safe 50 MiB constant so all pre-existing callers
    stay guarded without threading settings explicitly; the webhook resync edge
    passes ``github_settings.max_tarball_bytes`` so operator overrides take effect.
    """
    report = SyncReport()
    report_lock = asyncio.Lock()

    # 1. Resolve PAT per-agent (D-25: no principal-default bleed on the agent path).
    # Find the MA agent to derive its per-agent UUID, then call get_pat with that
    # agent_id. If the agent is not found on MA (shouldn't happen for a sync target),
    # fall back to agent_id=None so public repos still work — but only as an explicit
    # last resort, never silently using the principal credential for a resolved agent.
    # PAT-optional: public repos work when get_pat returns None (the fetcher returns
    # 404 for private repos, handled below as `skipped_repos`).
    ma_agent = await find_agent_by_daimon_tag(
        anthropic_client, tenant_id=tenant_id, name=agent_name
    )
    resolved_agent_id: uuid.UUID | None
    if ma_agent is not None:
        resolved_agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
    else:
        # Agent not found on MA — use agent_id=None (principal-default) only as
        # a safe fallback for public repos. Private repos will 404 as skipped_repos.
        resolved_agent_id = None
    # WR-01: when the caller pre-selected a credential, it is the single authority —
    # do NOT re-resolve a per-agent PAT (that double-resolution is what silently
    # shadowed the App installation token).
    if credential_override is not None:
        pat = credential_override
    else:
        pat = await get_pat(
            principal_id=principal_id,
            agent_id=resolved_agent_id,
            sessionmaker=sessionmaker,
            fernet=fernet,
        )

    # CR-01: user_skills is a per-AGENT ledger, not a per-user one. Both the panel
    # sync (Discord-user account principal) and the webhook resync (synthetic webhook
    # principal) target the SAME (tenant, agent). Keying the ledger on the agent's
    # stable derived identity makes both paths share one ledger — so dedup and
    # orphan-delete operate on the same rowset. When the agent is not on MA
    # (resolved_agent_id is None, public-repo fallback), fall back to the caller's
    # principal_id so the ledger key stays non-null.
    ledger_principal_id = resolved_agent_id if resolved_agent_id is not None else principal_id

    fetcher = GitHubTarballFetcher(http_client, max_tarball_bytes=max_tarball_bytes)

    # 2. Per-repo: fetch + bundle, accumulate pending skills.
    successfully_fetched: set[str] = set()
    pending: dict[str, _PendingSkill] = {}

    with tempfile.TemporaryDirectory(prefix="daimon-skill-sync-") as tmp_root:
        tmp_root_path = Path(tmp_root)
        for repo in repos:
            try:
                tarball = await fetcher.fetch_tarball(pat=pat, url=repo.url, branch=repo.branch)
            except (GitHubAuthError, GitHubUnreachable, TarballTooLarge) as err:
                report.skipped_repos.append((repo.url, type(err).__name__))
                continue
            except Exception as err:  # noqa: BLE001 — boundary
                _log.warning("skill_sync.repo_fetch_failed", url=repo.url, error=str(err))
                report.skipped_repos.append((repo.url, str(err)))
                continue

            extract_root = tmp_root_path / hashlib.sha256(repo.url.encode()).hexdigest()
            extract_root.mkdir(parents=True, exist_ok=True)
            # Owner-qualified bundled name: take the last two path segments so two
            # repos with the same trailing name (e.g. orgA/skills + orgB/skills) do
            # not collide on the same user_skills PK.  split=True names per-file and
            # is unaffected.  Example: github.com/orgA/skills → "orgA-skills".
            _stripped = repo.url.rstrip("/")
            _segments = _stripped.rsplit("/", 2)
            if len(_segments) >= 3:
                repo_name = _segments[-2] + "-" + _segments[-1].removesuffix(".git")
            else:
                repo_name = _segments[-1].removesuffix(".git")

            try:
                entries: list[SkillEntry] = await extract_and_bundle(
                    tarball_bytes=tarball,
                    extract_root=extract_root,
                    repo_name=repo_name,
                    split=repo.split,
                )
            except Exception as err:  # noqa: BLE001 — boundary
                _log.warning("skill_sync.repo_bundle_failed", url=repo.url, error=str(err))
                report.skipped_repos.append((repo.url, str(err)))
                continue

            successfully_fetched.add(repo.url)

            for entry in entries:
                if entry.skip_reason is not None:
                    report.failed_uploads.append((entry.name, entry.skip_reason))
                    continue
                if entry.name in pending:
                    raise RepoCollisionError(
                        f"skill name {entry.name!r} appears in "
                        f"{pending[entry.name].repo_url} and {repo.url}"
                    )
                pending[entry.name] = _PendingSkill(
                    name=entry.name,
                    repo_url=repo.url,
                    repo_branch=repo.branch,
                    repo_path=repo.path,
                    skill_dir=entry.skill_dir,
                    prebuilt_zip=entry.prebuilt_zip,
                )

        # 3. Bounded-concurrent upload (in deterministic name order for test stability).
        ordered = [pending[name] for name in sorted(pending.keys())]
        await _upload_all(
            pending_skills=ordered,
            principal_id=ledger_principal_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
            sessionmaker=sessionmaker,
            anthropic_client=anthropic_client,
            report=report,
            report_lock=report_lock,
        )

    # 4. Orphan delete — only for rows whose source repo was successfully
    # fetched this run. Rows tied to a transient-outage repo are left alone.
    async with sessionmaker() as session, session.begin():
        existing_rows = await list_user_skills_for_agent(
            session,
            tenant_id=tenant_id,
            principal_id=ledger_principal_id,
            agent_name=agent_name,
        )
    for row in existing_rows:
        if row.source_repo_url not in successfully_fetched:
            continue
        if row.name in pending:
            continue
        # Best-effort MA delete; failure is recorded but does not block local
        # row removal (the user_skills row exists to track our view of MA;
        # if MA still has the skill, leaving the local row would cause a
        # stale dedup mismatch on the next sync).
        if row.anthropic_id is not None:
            try:
                await anthropic_client.beta.skills.delete(row.anthropic_id)
            except anthropic.APIStatusError as err:
                _log.warning(
                    "skill_sync.orphan_delete_ma_failed",
                    name=row.name,
                    anthropic_id=row.anthropic_id,
                    error=str(err),
                )
                async with report_lock:
                    report.failed_uploads.append((row.name, str(err)))
        async with sessionmaker() as session, session.begin():
            await delete_user_skill(
                session,
                tenant_id=tenant_id,
                principal_id=ledger_principal_id,
                agent_name=agent_name,
                name=row.name,
            )
        report.deleted += 1

    # 5. Attach step: patch agent.skills to (existing ∪ user_skill anthropic_ids).
    # Fixes #40 — the Discord Add Skill modal previously uploaded skills but
    # never bound them to the agent.
    #
    # Short-circuit when there are no tracked user_skills for this agent: the
    # union with [] equals on-MA state by construction, so neither the agent
    # lookup nor the agents.update call is needed.
    async with sessionmaker() as session, session.begin():
        current_rows = await list_user_skills_for_agent(
            session,
            tenant_id=tenant_id,
            principal_id=ledger_principal_id,
            agent_name=agent_name,
        )

    if not any(row.anthropic_id is not None for row in current_rows):
        return report

    agent = await find_agent_by_daimon_tag(anthropic_client, tenant_id=tenant_id, name=agent_name)
    if agent is None:
        _log.warning(
            "skill_sync.attach_skipped_no_agent",
            agent_name=agent_name,
            tenant_id=str(tenant_id),
        )
        return report

    # Pre-check using the initially-fetched agent for a cheap no-op early return.
    # A concurrent change is exactly what the retry below covers — if the initial
    # check passes, proceed; the closure will recompute from the fresh agent.
    row_ids: list[str] = [row.anthropic_id for row in current_rows if row.anthropic_id is not None]
    if _compute_skills_union_list(agent.skills, row_ids) is None:
        _log.info(
            "skill_sync.attach_noop",
            agent_name=agent_name,
            skill_count=len(agent.skills),
        )
        return report

    # Retry closure: recomputes skills union from `fresh` so a concurrent update
    # doesn't cause us to clobber a skill added between our initial read and now
    # (#144-2). `row_ids` (DB state) stays outside the closure — it doesn't change.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        union_list = _compute_skills_union_list(fresh.skills, row_ids)
        if union_list is None:
            # Race: another writer already attached all skills; return fresh agent
            # unchanged (version bump already happened; no update needed).
            return fresh

        # #141 guard: if the agent lacks a base agent_toolset, attach it now so
        # skills remain usable. MA rejects session creation on an agent with skills
        # but no agent_toolset_20260401 entry providing the read tool.
        has_base_toolset = any(
            t.type == "agent_toolset_20260401"
            for t in fresh.tools  # type: ignore[union-attr]  # BetaManagedAgentsAgentToolset20260401 has .type; union members all have it
        )
        if not has_base_toolset:
            tools_arg = merge_default_agent_toolset(
                [t.model_dump(mode="json", exclude_none=True) for t in fresh.tools]  # type: ignore[arg-type]  # dumped dicts satisfy Tool TypedDict shape
            )
            return await anthropic_client.beta.agents.update(
                fresh.id,
                version=fresh.version,
                skills=union_list,
                tools=tools_arg,  # type: ignore[arg-type]  # list[dict] satisfies list[Tool] at runtime
            )
        return await anthropic_client.beta.agents.update(
            fresh.id,
            version=fresh.version,
            skills=union_list,
        )

    try:
        updated = await update_agent_with_version_retry(anthropic_client, agent.id, _apply)
    except anthropic.APIStatusError as err:
        if not _looks_like_skill_cap(err):
            raise
        # Uploads already landed; only the agents.update binding was refused
        # because the union exceeds the org's per-agent skill cap. Surface an
        # actionable failure instead of letting the raw 400 abort the whole sync
        # (the user must remove a skill repo — see remove_agent_skill_repo).
        _log.warning(
            "skill_sync.attach_over_cap",
            agent_name=agent_name,
            agent_id=agent.id,
            requested=len(row_ids),
            error=err.message,
        )
        async with report_lock:
            report.attach_failures.append((agent_name, err.message or "skill cap exceeded"))
        return report

    _log.info(
        "skill_sync.attach_done",
        agent_name=agent_name,
        agent_id=agent.id,
        skill_count=len(updated.skills),
    )

    return report
