"""Skill repositories: mirror the skills a repository holds into the library, and keep them current.

A repository's skills are mirrors like a skills server's, so an update that adds or changes a
runnable file waits for an admin's approval (`server_skills.gate`); from a repository the
workspace doesn't reach through its GitHub App, every change does. `SkillRepoLoader` is the
seam for declared repositories: the data package's declared file fills it, and
`sync_declared_skill_repos` applies what it returns.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

import httpx
import structlog
from daimon.core import ports
from daimon.core.agent_editing import AgentEditDeps, AgentEditError
from daimon.core.config import GithubAppSettings, GithubSettings, Settings
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.server_skills.repo import RepoUpstream, parse_repo_server, repo_server
from daimon.core.server_skills.source import SystemWriter, source_deps
from daimon.core.server_skills.sync import (
    SyncDeps,
    Upstream,
    UpstreamGone,
    UpstreamUnreachable,
    check_mirror,
)
from daimon.core.skill_library_files import SkillWriter
from daimon.core.stores import server_skills as mirrors_store
from daimon.core.stores import workspace_skills as workspace_skills_store
from daimon.core.stores.domain import ServerSkillRow

__all__ = [
    "RepoSkillOutcome",
    "SkillRepoDecl",
    "SkillRepoLoader",
    "Upstreams",
    "add_repo_skills",
    "mirror_sync_deps",
    "register_skill_repo_loader",
    "repo_upstream",
    "resync_repo_mirrors",
    "skill_repo_loader",
    "sync_declared_skill_repos",
]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SkillRepoDecl:
    """A repository a workspace takes skills from."""

    tenant_id: uuid.UUID
    repo: str
    """`owner/name`."""
    branch: str
    paths: tuple[str, ...] = ()
    """Folders to take skills from; none means the whole repository."""
    scope: str | None = None
    """Who may read them (a skill scope), or None for everyone who may use the library."""


class SkillRepoLoader(Protocol):
    """Where declared skill repositories come from; the declared configuration implements it."""

    async def declared(self) -> Sequence[SkillRepoDecl]: ...


_LOADER: Final[list[Callable[[Settings], SkillRepoLoader]]] = []


def register_skill_repo_loader(factory: Callable[[Settings], SkillRepoLoader]) -> None:
    """Fill the declared repositories seam (the data package's declared file)."""
    _LOADER[:] = [factory]


def skill_repo_loader(settings: Settings) -> SkillRepoLoader | None:
    """The registered loader, or None where no package declares repositories."""
    ports.load()
    return _LOADER[0](settings) if _LOADER else None


type Upstreams = dict[tuple[uuid.UUID, str], RepoUpstream]
"""Repositories read in one pass, by (tenant, server): each is downloaded once."""


def repo_upstream(
    github: GithubSettings,
    github_app: GithubAppSettings | None,
    edit: AgentEditDeps,
    *,
    tenant_id: uuid.UUID,
    repo: str,
    branch: str,
    http: httpx.AsyncClient,
    now: Callable[[], datetime],
    upstreams: Upstreams | None = None,
) -> RepoUpstream:
    """The repository branch read for a workspace, shared through `upstreams` when given."""
    key = (tenant_id, repo_server(repo, branch))
    if upstreams is not None and key in upstreams:
        return upstreams[key]
    upstream = RepoUpstream(
        tenant_id=tenant_id,
        repo=repo,
        branch=branch,
        sessionmaker=edit.session_factory,
        http=http,
        app_id=None if github_app is None else github_app.app_id,
        app_private_key=None if github_app is None else github_app.private_key,
        now=now,
        max_tarball_bytes=github.max_tarball_bytes,
        max_tarball_decompressed_bytes=github.max_tarball_decompressed_bytes,
    )
    if upstreams is not None:
        upstreams[key] = upstream
    return upstream


def mirror_sync_deps(
    mirror: ServerSkillRow,
    github: GithubSettings,
    github_app: GithubAppSettings | None,
    edit: AgentEditDeps,
    *,
    http: httpx.AsyncClient,
    now: Callable[[], datetime],
    local: Mapping[str, Upstream] | None = None,
    upstreams: Upstreams | None = None,
) -> SyncDeps | None:
    """The sync that keeps an in-process mirror current (a repository's or a local store's);
    None for a skills server's, which its node keeps. Every adapter asks here."""
    parsed = parse_repo_server(mirror.server)
    if parsed is None:
        source = (local or {}).get(mirror.server)
        if source is None:
            return None
        return source_deps(server=mirror.server, upstream=source, edit=edit, now=now)
    upstream = repo_upstream(
        github,
        github_app,
        edit,
        tenant_id=mirror.tenant_id,
        repo=parsed[0],
        branch=parsed[1],
        http=http,
        now=now,
        upstreams=upstreams,
    )
    return source_deps(
        server=mirror.server,
        upstream=upstream,
        edit=edit,
        now=now,
        approve_every_change=upstream.anonymous,
    )


@dataclass(frozen=True)
class RepoSkillOutcome:
    name: str
    action: Literal["added", "kept", "failed"]
    detail: str | None = None


def _wanted(folder: str, paths: Sequence[str]) -> bool:
    roots = [p.strip("/") for p in paths if p.strip("/")]
    return not roots or any(folder == r or folder.startswith(f"{r}/") for r in roots)


async def add_repo_skills(
    deps: AgentEditDeps,
    upstream: RepoUpstream,
    decl: SkillRepoDecl,
    *,
    writer: SkillWriter,
    now: datetime,
) -> list[RepoSkillOutcome]:
    """Mirror each skill of the repository's folders that the library doesn't mirror yet.

    One skill the library refuses (a taken name, a failed check, a scope it can't save) is
    reported and the rest still land. A new skill is created with the declaration's scope; a
    kept one takes it only when the declaration names one.
    """
    server = repo_server(decl.repo, decl.branch)
    outcomes: list[RepoSkillOutcome] = []
    for entry in await upstream.listing(tenant_id=decl.tenant_id):
        folder = entry.uri.removeprefix("repo:///").removesuffix("/SKILL.md")
        if not _wanted(folder, decl.paths):
            continue
        async with deps.session_factory() as session:
            held = await mirrors_store.find_mirror(
                session, tenant_id=decl.tenant_id, server=server, uri=entry.uri
            )
        try:
            if held is None:
                await add_server_skill(
                    deps,
                    actor=writer,
                    server=server,
                    fetched=await upstream.fetch_uri(entry.uri),
                    now=now,
                    scope=decl.scope,
                )
                action: Literal["added", "kept"] = "added"
            else:
                await _keep_scope(deps, decl, held.skill_id)
                action = "kept"
        except AgentEditError as err:
            outcomes.append(RepoSkillOutcome(entry.name, "failed", str(err)))
            continue
        outcomes.append(RepoSkillOutcome(entry.name, action))
    return outcomes


async def _keep_scope(deps: AgentEditDeps, decl: SkillRepoDecl, skill_id: str) -> None:
    if decl.scope is None:
        return
    async with deps.session_factory() as session, session.begin():
        if not await workspace_skills_store.set_scope(
            session, tenant_id=decl.tenant_id, skill_id=skill_id, scope=decl.scope
        ):
            raise AgentEditError("The skill's scope could not be saved.", kind="unavailable")


async def sync_declared_skill_repos(
    loader: SkillRepoLoader,
    *,
    edit: AgentEditDeps,
    upstream_for: Callable[[SkillRepoDecl], RepoUpstream],
    now: Callable[[], datetime],
) -> dict[str, list[RepoSkillOutcome]]:
    """Apply every declared repository: add its new skills and set their scope. A repository
    that can't be read is logged and tried on the next pass.

    Existing mirrors are kept current by the check loop and the push queue, not here.
    """
    out: dict[str, list[RepoSkillOutcome]] = {}
    for decl in await loader.declared():
        key = f"{decl.tenant_id}:{repo_server(decl.repo, decl.branch)}"
        try:
            out[key] = await add_repo_skills(
                edit, upstream_for(decl), decl, writer=SystemWriter(decl.tenant_id), now=now()
            )
        except (UpstreamGone, UpstreamUnreachable) as err:
            log.warning("skill_repo.declared_unreadable", repo=decl.repo, err=type(err).__name__)
    return out


async def resync_repo_mirrors(
    *,
    repo_full_name: str,
    ref: str,
    github: GithubSettings,
    github_app: GithubAppSettings | None,
    edit: AgentEditDeps,
    http: httpx.AsyncClient,
    now: Callable[[], datetime],
) -> tuple[int, int]:
    """Check each mirror of a pushed repository branch; `(failed, retryable)` counts.

    Only a repository GitHub didn't serve (unreachable, rate limited) is worth retrying: a
    folder that fails its checks is reported on its mirror and waits for the next push. An
    update held for approval is not a failure.
    """
    async with edit.session_factory() as session:
        mirrors = [
            m
            for m in await mirrors_store.list_mirrors(session)
            if (parsed := parse_repo_server(m.server)) is not None
            and parsed[0].casefold() == repo_full_name.casefold()
            and ref == f"refs/heads/{parsed[1]}"
        ]
    upstreams: Upstreams = {}
    down: set[tuple[uuid.UUID, str]] = set()
    failed = retryable = 0
    for mirror in mirrors:
        deps = mirror_sync_deps(
            mirror, github, github_app, edit, http=http, now=now, upstreams=upstreams
        )
        key = (mirror.tenant_id, mirror.server)
        if deps is None:
            continue
        if key not in down:
            try:
                await upstreams[key].listing(tenant_id=mirror.tenant_id)
            except UpstreamUnreachable:
                down.add(key)
            except UpstreamGone:
                pass  # the check marks the mirror unavailable
        if key in down:
            failed, retryable = failed + 1, retryable + 1
        elif await check_mirror(deps, mirror.id) == "failed":
            failed += 1
    log.info("skill_repo.resynced", repo=repo_full_name, mirrors=len(mirrors), failed=failed)
    return failed, retryable
