"""One agent's GitHub panel: its repos, where its skills come from, sync state and release.

Reads degrade: a failed provider call leaves a field empty rather than failing the
panel. Changes reuse the stores the chat tools call, so they audit the same way.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.skill_list_response import SkillListResponse
from cryptography.fernet import MultiFernet
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import AgentTarget, ChangeRecorder, load_target
from daimon.core.defaults.ma_index import list_skills_lenient
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SOURCE_RELEASE,
    MA_METADATA_KEY_SOURCE_RELEASE_AT,
    MA_METADATA_KEY_SOURCE_RELEASE_URL,
    strip_tenant_prefix,
)
from daimon.core.github_credentials import encrypt_token
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.ma_resolver import list_skills_lenient_cached
from daimon.core.mux_compat import list_skill_versions
from daimon.core.skill_library import SyncedSkill, github_url_of
from daimon.core.skill_sync.orchestrator import SyncReport, sync_agent_skills
from daimon.core.skill_sync.resync_lock import hold_resync_locks
from daimon.core.specs import SkillRepo
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import GitHubPushResyncRow, Role, UserSkillRow
from daimon.core.stores.github_access import (
    AgentRepo,
    InstallationLimitError,
    ReadOnlyMultiInstallationError,
    SessionVaultBudgetError,
    active_grant_for_repo,
    list_agent_repos,
    remove_agent_repo,
    repo_id_for_agent_removal,
    set_working_repo,
)
from daimon.core.stores.github_connect import (
    PinnedAgentConnectionError,
    mint_invitation,
    require_app_eligible_agent,
    set_invitation_encrypted_token,
)
from daimon.core.stores.github_push_resync import get_latest_for_repo_refs
from daimon.core.stores.integration_reads import get_installation_states
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.user_skills import list_user_skills_for_agent_name
from daimon.core.stores.web_people import get_people_for_accounts
from mux.errors import MuxError
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

ASK_WHO = "Ask an admin or the agent's owner."
NOT_CONFIGURED = "GitHub is not set up here. Ask an admin."
# Version lookups run only for skills pinned behind their latest; a long list of
# such pins must not fan out into one provider call each.
_VERSION_LOOKUPS = 12
_VERSION_CONCURRENCY = 4
_VERSION_PAGE = 50
_VERSION_TIMES_TTL = timedelta(minutes=5)
_VERSION_TIMES_CAP = 2048

type _VersionKey = tuple[uuid.UUID, str, str]


class VersionTimeCache:
    """When each skill version was made, per workspace, for a few minutes.

    A panel read otherwise walks up to a dozen version listings. A version not on the
    first page is kept as unknown too; a failed lookup is not kept.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = _VERSION_TIMES_TTL,
        cap: int = _VERSION_TIMES_CAP,
    ) -> None:
        self._now, self._ttl, self._cap = now, ttl, cap
        self._rows: dict[_VersionKey, tuple[datetime, datetime | None]] = {}

    def get(self, key: _VersionKey) -> tuple[bool, datetime | None]:
        """(known, time): known is False when the key was never stored or has expired."""
        row = self._rows.get(key)
        if row is None or row[0] <= self._now():
            self._rows.pop(key, None)
            return False, None
        return True, row[1]

    def put(self, key: _VersionKey, at: datetime | None) -> None:
        if key not in self._rows and len(self._rows) >= self._cap:
            del self._rows[next(iter(self._rows))]
        self._rows[key] = (self._now() + self._ttl, at)


Origin = Literal["repo", "library", "external", "anthropic"]
InstallationState = Literal["active", "suspended", "removed"]


class PanelRepo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    access: Literal["read", "write"]
    working: bool
    shared: bool
    status: Literal["active", "suspended", "revoked"]
    installation: InstallationState
    granted_at: datetime | None
    granted_by: str | None


class PanelSync(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["pending", "running", "done"]
    at: datetime
    error: str | None


class PanelSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: str
    title: str
    version: str | None
    origin: Origin
    repo: str | None = None
    branch: str | None = None
    path: str | None = None
    url: str | None = None
    updated_at: datetime | None = None


class PanelRelease(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    at: datetime | None
    url: str | None


class GitHubPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    can_manage: bool
    can_connect: bool
    repos: list[PanelRepo]
    sync: PanelSync | None
    skills: list[PanelSkill]
    release: PanelRelease | None


class ResyncResults(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: list[SyncedSkill]


@dataclass(frozen=True)
class RepoSource:
    """Where some of an agent's skills are synced from."""

    repo: str
    url: str
    branch: str
    path: str
    principal_id: uuid.UUID


@dataclass(frozen=True)
class SkillResyncDeps:
    fernet: MultiFernet
    http_client: httpx.AsyncClient
    app_id: str | None
    app_private_key: SecretStr | None
    max_tarball_bytes: int
    max_tarball_decompressed_bytes: int


# ---- pure assembly -------------------------------------------------------------


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def parse_release(metadata: Mapping[str, str]) -> PanelRelease | None:
    """The release an outside pipeline stamped; a bad time or a non-https URL reads as unset."""
    name = (metadata.get(MA_METADATA_KEY_SOURCE_RELEASE) or "").strip()
    if not name:
        return None
    url = (metadata.get(MA_METADATA_KEY_SOURCE_RELEASE_URL) or "").strip()
    safe_url = url if url.startswith("https://") and " " not in url and len(url) > 8 else None
    return PanelRelease(
        name=name[:200],
        at=_parse_time(metadata.get(MA_METADATA_KEY_SOURCE_RELEASE_AT)),
        url=safe_url,
    )


def agent_name_of(agent: BetaManagedAgentsAgent) -> str:
    """The name the skill ledger and push resync key an agent by."""
    return agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name


def panel_repos(
    repos: Sequence[AgentRepo],
    installations: Mapping[int, InstallationState],
    granters: Mapping[uuid.UUID, str],
) -> list[PanelRepo]:
    """The agent's repos, the working one first, then by name."""
    out = [
        PanelRepo(
            name=repo.full_name,
            url=f"https://github.com/{repo.full_name}",
            access=repo.max_access,
            working=repo.is_working_repo,
            shared=repo.scope == "shared",
            status=repo.status,
            # A repo row outlives the installation record it named: treat it as gone.
            installation=installations.get(repo.installation_id, "removed")
            if repo.installation_id is not None
            else "removed",
            granted_at=repo.granted_at,
            granted_by=granters.get(repo.granted_by_account_id)
            if repo.granted_by_account_id is not None
            else None,
        )
        for repo in repos
    ]
    return sorted(out, key=lambda r: (not r.working, r.name.casefold()))


def panel_sync(row: GitHubPushResyncRow | None) -> PanelSync | None:
    if row is None or row.state not in ("pending", "running", "done"):
        return None
    state: Literal["pending", "running", "done"] = (
        "pending" if row.state == "pending" else "running" if row.state == "running" else "done"
    )
    # The job is shared by every workspace on the repo, so its raw error (binding
    # counts across them) never leaves the server; a failed pass is always retried.
    error = "The last sync failed. It will try again." if row.last_error else None
    return PanelSync(state=state, at=row.updated_at, error=error)


def _ledger(rows: Sequence[UserSkillRow], agent_id: uuid.UUID) -> dict[str, UserSkillRow]:
    """Rows by skill id; the agent-keyed row wins over an older user-keyed one."""
    out: dict[str, UserSkillRow] = {}
    for row in rows:
        if row.anthropic_id is None:
            continue
        if row.anthropic_id not in out or row.principal_id == agent_id:
            out[row.anthropic_id] = row
    return out


def _looks_tenant_prefixed(title: str) -> bool:
    head = title[:9]
    return len(head) == 9 and head[8] == "-" and all(c in "0123456789abcdef" for c in head[:8])


def panel_skills(
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    rows: Sequence[UserSkillRow],
    listing: Sequence[SkillListResponse],
    version_times: Mapping[tuple[str, str], datetime],
) -> list[PanelSkill]:
    """Each attached skill with its origin; repo skills carry their source. By title."""
    ledger = _ledger(rows, agent_id)
    listed = {row.id: row for row in listing}
    out: list[PanelSkill] = []
    for pinned in agent.skills:
        skill_id = pinned.skill_id
        pin = pinned.version if pinned.version not in ("", "latest") else None
        meta = listed.get(skill_id)
        version = pin or (meta.latest_version if meta is not None else None)
        updated = (
            _parse_time(meta.updated_at)
            if meta is not None and (pin is None or pin == meta.latest_version)
            else version_times.get((skill_id, pin))
            if pin is not None
            else None
        )
        row = ledger.get(skill_id)
        if pinned.type == "anthropic":
            out.append(
                PanelSkill(
                    skill_id=skill_id,
                    title=skill_id,
                    version=version,
                    origin="anthropic",
                    updated_at=updated,
                )
            )
            continue
        bare = (
            strip_tenant_prefix(tenant_id=tenant_id, display_title=meta.display_title)
            if meta is not None and meta.display_title
            else None
        )
        if row is not None and row.source == "repo":
            repo = normalize_owner_repo(row.source_repo_url)
            out.append(
                PanelSkill(
                    skill_id=skill_id,
                    title=row.name,
                    version=version,
                    origin="repo",
                    repo=repo,
                    branch=row.source_repo_branch,
                    path=row.source_path,
                    url=github_url_of(
                        f"https://github.com/{repo}", row.source_repo_branch, row.source_path
                    ),
                    updated_at=row.updated_at,
                )
            )
            continue
        if row is not None:
            title = row.name
        elif bare is not None:
            title = bare.rpartition("/")[2] or bare
        elif (
            meta is not None
            and meta.display_title
            and not _looks_tenant_prefixed(meta.display_title)
        ):
            title = meta.display_title.rpartition("/")[2] or meta.display_title
        else:
            title = skill_id
        out.append(
            PanelSkill(
                skill_id=skill_id,
                title=title,
                version=version,
                origin="library" if row is not None or bare is not None else "external",
                updated_at=updated,
            )
        )
    return sorted(out, key=lambda s: (s.title.casefold(), s.skill_id))


def repo_sources(
    agent: BetaManagedAgentsAgent, rows: Sequence[UserSkillRow], agent_id: uuid.UUID
) -> list[RepoSource]:
    """The distinct repo, branch and path the agent's attached repo skills sync from."""
    attached = {s.skill_id for s in agent.skills if s.type == "custom"}
    seen: dict[tuple[str, str, str], RepoSource] = {}
    for skill_id, row in _ledger(rows, agent_id).items():
        if skill_id not in attached or row.source != "repo":
            continue
        key = (row.source_repo_url, row.source_repo_branch, row.source_path)
        if key not in seen:
            seen[key] = RepoSource(
                repo=normalize_owner_repo(row.source_repo_url),
                url=row.source_repo_url,
                branch=row.source_repo_branch,
                path=row.source_path,
                principal_id=row.principal_id,
            )
    return sorted(seen.values(), key=lambda s: (s.repo.casefold(), s.branch, s.path))


def resync_results(
    source: RepoSource,
    before: Sequence[UserSkillRow],
    after: Sequence[UserSkillRow],
    report: SyncReport,
    *,
    agent_id: uuid.UUID,
) -> list[SyncedSkill]:
    """What one source's sync did to each skill, from the ledger before and after it."""

    def of(rows: Sequence[UserSkillRow]) -> dict[str, tuple[str, str | None]]:
        out: dict[str, tuple[str, str | None]] = {}
        for r in sorted(rows, key=lambda r: r.principal_id == agent_id):
            if (r.source_repo_url, r.source_repo_branch, r.source_path) == (
                source.url,
                source.branch,
                source.path,
            ):
                out[r.name] = (r.content_hash, r.anthropic_latest_version)
        return out

    old, new = of(before), of(after)
    failed = {name: reason[:200] for name, reason in report.failed_uploads}
    results: list[SyncedSkill] = []
    if any(url == source.url for url, _ in report.skipped_repos):
        results.append(
            SyncedSkill(name=source.repo, action="failed", detail="The repo could not be read.")
        )
    for name in sorted(old.keys() | new.keys() | failed.keys()):
        if name in failed:
            results.append(SyncedSkill(name=name, action="failed", detail=failed[name]))
        elif name not in old:
            results.append(SyncedSkill(name=name, action="created"))
        elif name not in new:
            results.append(
                SyncedSkill(name=name, action="skipped", detail="Removed from the repo.")
            )
        elif old[name] != new[name]:
            results.append(SyncedSkill(name=name, action="updated"))
        else:
            results.append(SyncedSkill(name=name, action="skipped", detail="Unchanged."))
    if report.attach_failures:
        results.append(
            SyncedSkill(
                name=source.repo,
                action="failed",
                detail="The skills could not be added to the agent.",
            )
        )
    return results


# ---- reads ----------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubAuthority:
    manage: bool
    connect: bool
    # The actor's chat account, which a connect link is pinned to.
    platform_user_id: str | None = None


async def github_authority(
    session: AsyncSession, actor: AgentActor, target: AgentTarget
) -> GitHubAuthority:
    """The agent's owner, or a platform admin, as GitHub's own checks judge them.

    GitHub never accepts delegated authority, so a web workspace role alone does not
    count. Connecting also needs the chat account a live admin check can attest: the
    connect link's confirm step refuses anyone else, so the panel never offers it.
    """
    if not target.sees_setup:
        return GitHubAuthority(manage=False, connect=False)
    identity = await get_account_with_tenant(session, account_id=actor.account_id)
    if identity is not None and identity.tenant_id != actor.tenant_id:
        identity = None
    user_id = identity.platform_user_id if identity is not None else None
    if target.owned and target.agent.metadata.get(MA_METADATA_KEY_MANAGED) != "true":
        return GitHubAuthority(manage=True, connect=True, platform_user_id=user_id)
    admin = identity is not None and identity.role is Role.ADMIN
    return GitHubAuthority(
        manage=admin, connect=admin and user_id is not None, platform_user_id=user_id
    )


async def _target(deps: AgentEditDeps, actor: AgentActor, ma_agent_id: str) -> AgentTarget:
    try:
        target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
        raise AgentEditError("Agents are unavailable right now.", kind="unavailable") from exc
    if not target.sees_setup:
        raise AgentEditError(ASK_WHO, kind="forbidden")
    return target


async def _listing(deps: AgentEditDeps) -> list[SkillListResponse]:
    try:
        if deps.resolver_cache is not None:
            rows, _ = await list_skills_lenient_cached(deps.client, deps.resolver_cache)
        else:
            rows, _ = await list_skills_lenient(deps.client)
    except (anthropic.APIError, httpx.HTTPError):
        _log.warning("agent_github.skills_listing_failed")
        return []
    return rows


async def _version_times(
    deps: AgentEditDeps,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    listing: Sequence[SkillListResponse],
    cache: VersionTimeCache | None,
) -> dict[tuple[str, str], datetime]:
    """When each skill pinned behind its latest version got that version; bounded."""
    latest = {row.id: row.latest_version for row in listing}
    wanted = [
        (s.skill_id, s.version)
        for s in agent.skills
        if s.type == "custom"
        and s.version not in ("", "latest")
        and s.skill_id in latest
        and latest[s.skill_id] != s.version
    ][:_VERSION_LOOKUPS]
    gate = asyncio.Semaphore(_VERSION_CONCURRENCY)
    found: dict[tuple[str, str], datetime] = {}

    async def look(skill_id: str, version: str) -> None:
        key = (actor.tenant_id, skill_id, version)
        known, at = cache.get(key) if cache is not None else (False, None)
        if not known:
            async with gate:
                try:
                    async for row in list_skill_versions(
                        deps.client, skill_id, limit=_VERSION_PAGE, scope=actor.scope()
                    ):
                        if row.version == version:
                            at = _parse_time(row.created_at)
                            break
                except (anthropic.AnthropicError, httpx.HTTPError, MuxError):
                    # Any provider or scope failure leaves the time empty: the panel still answers.
                    _log.warning("agent_github.version_lookup_failed", skill_id=skill_id)
                    return
            if cache is not None:
                cache.put(key, at)
        if at is not None:
            found[(skill_id, version)] = at

    await asyncio.gather(*(look(skill_id, version) for skill_id, version in wanted))
    return found


async def _panel(
    deps: AgentEditDeps,
    actor: AgentActor,
    target: AgentTarget,
    *,
    configured: bool,
    version_times: VersionTimeCache | None,
) -> GitHubPanel:
    agent = target.agent
    async with deps.session_factory() as session:
        repos = await list_agent_repos(session, tenant_id=actor.tenant_id, agent_id=target.agent_id)
        installations = await get_installation_states(
            session,
            installation_ids={r.installation_id for r in repos if r.installation_id is not None},
        )
        people = await get_people_for_accounts(
            session,
            tenant_id=actor.tenant_id,
            account_ids={r.granted_by_account_id for r in repos if r.granted_by_account_id},
        )
        rows = await list_user_skills_for_agent_name(
            session, tenant_id=actor.tenant_id, agent_name=agent_name_of(agent)
        )
        sources = repo_sources(agent, rows, target.agent_id)
        authority = await github_authority(session, actor, target)
        sync = await get_latest_for_repo_refs(
            session,
            tenant_id=actor.tenant_id,
            repo_refs={(s.repo, f"refs/heads/{s.branch}") for s in sources},
        )
    listing = await _listing(deps)
    times = await _version_times(deps, actor, agent, listing, version_times)
    return GitHubPanel(
        configured=configured,
        can_manage=authority.manage,
        can_connect=authority.connect,
        repos=panel_repos(repos, installations, {a: p.display_name for a, p in people.items()}),
        sync=panel_sync(sync),
        skills=panel_skills(
            agent,
            tenant_id=actor.tenant_id,
            agent_id=target.agent_id,
            rows=rows,
            listing=listing,
            version_times=times,
        ),
        release=parse_release(agent.metadata),
    )


async def get_panel(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    configured: bool,
    version_times: VersionTimeCache | None = None,
) -> GitHubPanel:
    """The panel for someone who sees the agent's setup (an admin, or its owner)."""
    target = await _target(deps, actor, ma_agent_id)
    return await _panel(deps, actor, target, configured=configured, version_times=version_times)


# ---- changes --------------------------------------------------------------------


async def _managed_target(
    deps: AgentEditDeps, actor: AgentActor, ma_agent_id: str, *, connecting: bool = False
) -> tuple[AgentTarget, GitHubAuthority]:
    target = await _target(deps, actor, ma_agent_id)
    async with deps.session_factory() as session:
        authority = await github_authority(session, actor, target)
    if not (authority.connect if connecting else authority.manage):
        raise AgentEditError(ASK_WHO, kind="forbidden")
    return target, authority


async def _audit(
    session: AsyncSession,
    actor: AgentActor,
    target: AgentTarget,
    *,
    tool: str,
    reason: str,
    audited: ChangeRecorder | None,
) -> None:
    await append_event(
        session,
        tenant_id=actor.tenant_id,
        account_id=actor.account_id,
        agent_id=target.agent_id,
        agent_name=target.agent.name,
        platform="web",
        platform_user_id=None,
        tool_name=tool,
        operation=tool,
        outcome="allowed",
        reason=reason,
    )
    if audited is not None:
        await audited(session, target.agent.name)


async def connect_url(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    root: str | None,
    fernet: MultiFernet,
    audited: ChangeRecorder | None = None,
) -> str:
    """A single-use Connect GitHub link for this agent only, minted for its manager."""
    target, authority = await _managed_target(deps, actor, ma_agent_id, connecting=True)
    if root is None:
        raise AgentEditError(NOT_CONFIGURED, kind="conflict")
    async with deps.session_factory() as session, session.begin():
        try:
            await require_app_eligible_agent(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent_id,
                agent_name=target.agent.name,
            )
        except PinnedAgentConnectionError as exc:
            raise AgentEditError(str(exc), kind="conflict") from exc
        token = await mint_invitation(
            session,
            tenant_id=actor.tenant_id,
            requester_account_id=actor.account_id,
            # Pins the link to this chat account, as the chat tool's link is.
            requester_platform_user_id=authority.platform_user_id,
            agent_id=target.agent_id,
            agent_name=target.agent.name,
            agent_ma_id=target.agent.id,
            agent_manager_verified=True,
            origin_platform="web",
        )
        await set_invitation_encrypted_token(
            session, token=token, encrypted_token=encrypt_token(fernet, token)
        )
        await _audit(
            session, actor, target, tool="github_connect", reason="link minted", audited=audited
        )
    return f"{root}/oauth/github/connect/{token}"


def _repo_name(repo: str) -> str:
    name = repo.strip()
    if name.count("/") != 1 or any(c.isspace() for c in name) or name.startswith("/"):
        raise AgentEditError("Name one repo as owner/repo.", kind="invalid")
    return name


async def set_working(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    repo: str | None,
    configured: bool,
    audited: ChangeRecorder | None = None,
    version_times: VersionTimeCache | None = None,
) -> GitHubPanel:
    """Make one of the agent's repos its working repo, or clear it with None."""
    target, _authority = await _managed_target(deps, actor, ma_agent_id)
    name = _repo_name(repo) if repo is not None else None
    async with deps.session_factory() as session, session.begin():
        try:
            await set_working_repo(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent_id,
                repo_name=name,
                account_id=actor.account_id,
                agent_name=target.agent.name,
            )
        except (
            InstallationLimitError,
            SessionVaultBudgetError,
            ReadOnlyMultiInstallationError,
        ) as exc:
            # Each says in one line what limit the change hits, as the chat tool relays it.
            raise AgentEditError(str(exc), kind="conflict") from exc
        except ValueError as exc:
            # The store's one plain ValueError: the repo is not a live grant of this agent.
            raise AgentEditError("That repo is not on this agent's list.", kind="invalid") from exc
        if audited is not None:
            await audited(session, target.agent.name)
    return await _panel(deps, actor, target, configured=configured, version_times=version_times)


async def remove_repo(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    repo: str,
    configured: bool,
    audited: ChangeRecorder | None = None,
    version_times: VersionTimeCache | None = None,
) -> GitHubPanel:
    """Take one repo off this agent; other agents keep theirs, a shared repo stays shared."""
    target, _authority = await _managed_target(deps, actor, ma_agent_id)
    name = _repo_name(repo)
    async with deps.session_factory() as session, session.begin():
        repo_id = await repo_id_for_agent_removal(
            session, tenant_id=actor.tenant_id, agent_id=target.agent_id, repo_name=name
        )
        if repo_id is None:
            raise AgentEditError("That repo is not on this agent's list.", kind="not_found")
        removed = await remove_agent_repo(
            session,
            tenant_id=actor.tenant_id,
            agent_id=target.agent_id,
            repo_id=repo_id,
            account_id=actor.account_id,
        )
        if not removed:
            raise AgentEditError("That repo is no longer on this agent's list.", kind="not_found")
        if audited is not None:
            await audited(session, target.agent.name)
    return await _panel(deps, actor, target, configured=configured, version_times=version_times)


async def resync(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    sync: SkillResyncDeps,
    audited: ChangeRecorder | None = None,
) -> ResyncResults:
    """Sync the agent's repo skills now from their recorded sources; a no-op change is skipped.

    Holds each source's push-queue lock throughout, so a push sync or another
    manual one of the same branch, in any process, gets a conflict instead.
    """
    target, _authority = await _managed_target(deps, actor, ma_agent_id)
    app_id, app_key = sync.app_id, sync.app_private_key
    if app_id is None or app_key is None:
        raise AgentEditError(NOT_CONFIGURED, kind="conflict")
    agent_name = agent_name_of(target.agent)
    async with deps.session_factory() as session:
        rows = await list_user_skills_for_agent_name(
            session, tenant_id=actor.tenant_id, agent_name=agent_name
        )
        sources = repo_sources(target.agent, rows, target.agent_id)
    results: list[SyncedSkill] = []
    refs = {(s.repo, f"refs/heads/{s.branch}") for s in sources}
    async with hold_resync_locks(deps.session_factory, refs) as free:
        if not free:
            raise AgentEditError("A sync is running. Try again when it ends.", kind="conflict")
        for source in sources:
            results.extend(
                await _resync_one(
                    deps, actor, target, source, sync, agent_name, app=(app_id, app_key)
                )
            )
    async with deps.session_factory() as session, session.begin():
        await _audit(
            session,
            actor,
            target,
            tool="github_resync",
            reason=f"{len(sources)} source(s) resynced",
            audited=audited,
        )
    return ResyncResults(results=results)


async def _resync_one(
    deps: AgentEditDeps,
    actor: AgentActor,
    target: AgentTarget,
    source: RepoSource,
    sync: SkillResyncDeps,
    agent_name: str,
    *,
    app: tuple[str, SecretStr],
) -> list[SyncedSkill]:
    async with deps.session_factory() as session:
        grant = await active_grant_for_repo(
            session, tenant_id=actor.tenant_id, agent_id=target.agent_id, repo_name=source.repo
        )
        before = await list_user_skills_for_agent_name(
            session, tenant_id=actor.tenant_id, agent_name=agent_name
        )
    if grant is None:
        return [
            SyncedSkill(
                name=source.repo, action="skipped", detail="This agent has no access to the repo."
            )
        ]
    try:
        report = await sync_agent_skills(
            principal_id=source.principal_id,
            tenant_id=actor.tenant_id,
            agent_name=agent_name,
            target_ma_agent_id=target.agent.id,
            repos=[SkillRepo(url=source.url, branch=source.branch, path=source.path)],
            sessionmaker=deps.session_factory,
            fernet=sync.fernet,
            http_client=sync.http_client,
            anthropic_client=deps.client,
            app_id=app[0],
            app_private_key=app[1],
            max_tarball_bytes=sync.max_tarball_bytes,
            max_tarball_decompressed_bytes=sync.max_tarball_decompressed_bytes,
        )
    # Boundary: one source failing must not stop the others or lose their results.
    except Exception as exc:
        _log.warning(
            "agent_github.resync_failed",
            repo=source.repo,
            agent_id=str(target.agent_id),
            error_type=type(exc).__name__,
        )
        return [SyncedSkill(name=source.repo, action="failed", detail="The sync failed.")]
    async with deps.session_factory() as session:
        after = await list_user_skills_for_agent_name(
            session, tenant_id=actor.tenant_id, agent_name=agent_name
        )
    return resync_results(source, before, after, report, agent_id=target.agent_id)
