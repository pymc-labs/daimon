"""Skill repositories: mirror the skills a repository holds into the library, and keep them current.

A repository's skills are mirrors like a skills server's, so an update that adds or changes a
runnable file waits for an admin's approval (`server_skills.gate`). `SkillRepoLoader` is the
seam for declared repositories: the declared configuration fills it, and
`sync_declared_skill_repos` applies what it returns.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import httpx
import structlog
from daimon.core.agent_editing import AgentEditDeps, AgentEditError
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.server_skills.repo import RepoUpstream, parse_repo_server, repo_server
from daimon.core.server_skills.source import SystemWriter, source_deps
from daimon.core.server_skills.sync import check_mirror
from daimon.core.skill_library_files import SkillWriter
from daimon.core.stores import server_skills as mirrors_store
from daimon.core.stores import workspace_skills as workspace_skills_store
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "RepoSkillOutcome",
    "SkillRepoDecl",
    "SkillRepoLoader",
    "add_repo_skills",
    "resync_repo_mirrors",
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

    One skill the library refuses (a taken name, a failed check) is reported and the rest
    still land. Every skill of the declaration, new or already mirrored, takes its scope.
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
            if held is not None:
                skill_id, action = held.skill_id, "kept"
            else:
                ref = await add_server_skill(
                    deps,
                    actor=writer,
                    server=server,
                    fetched=await upstream.fetch_uri(entry.uri),
                    now=now,
                )
                skill_id, action = ref.id, "added"
        except AgentEditError as err:
            outcomes.append(RepoSkillOutcome(entry.name, "failed", str(err)))
            continue
        async with deps.session_factory() as session, session.begin():
            await workspace_skills_store.set_scope(
                session, tenant_id=decl.tenant_id, skill_id=skill_id, scope=decl.scope
            )
        outcomes.append(RepoSkillOutcome(entry.name, action))
    return outcomes


async def sync_declared_skill_repos(
    loader: SkillRepoLoader,
    *,
    edit: AgentEditDeps,
    upstream_for: Callable[[SkillRepoDecl], RepoUpstream],
    now: Callable[[], datetime],
) -> dict[str, list[RepoSkillOutcome]]:
    """Apply every declared repository: add its new skills and set their scope.

    Existing mirrors are kept current by the check loop and the push queue, not here.
    """
    out: dict[str, list[RepoSkillOutcome]] = {}
    for decl in await loader.declared():
        out[f"{decl.tenant_id}:{repo_server(decl.repo, decl.branch)}"] = await add_repo_skills(
            edit,
            upstream_for(decl),
            decl,
            writer=SystemWriter(decl.tenant_id),
            now=now(),
        )
    return out


async def resync_repo_mirrors(
    *,
    repo_full_name: str,
    ref: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    edit: AgentEditDeps,
    http: httpx.AsyncClient,
    app_id: str | None,
    app_private_key: SecretStr | None,
    now: Callable[[], datetime],
) -> tuple[int, int]:
    """Check each mirror of a pushed repository branch; `(failed, retryable)` counts.

    A mirror whose files are only text updates at once; one that adds or changes a runnable
    file is held for approval, which is not a failure.
    """
    async with sessionmaker() as session:
        mirrors = [
            m
            for m in await mirrors_store.list_mirrors(session)
            if (parsed := parse_repo_server(m.server)) is not None
            and parsed[0].casefold() == repo_full_name.casefold()
            and ref == f"refs/heads/{parsed[1]}"
        ]
    upstreams: dict[tuple[uuid.UUID, str], RepoUpstream] = {}
    failed = 0
    for mirror in mirrors:
        parsed = parse_repo_server(mirror.server)
        if parsed is None:
            continue
        upstream = upstreams.setdefault(
            (mirror.tenant_id, mirror.server),
            RepoUpstream(
                tenant_id=mirror.tenant_id,
                repo=parsed[0],
                branch=parsed[1],
                sessionmaker=sessionmaker,
                http=http,
                app_id=app_id,
                app_private_key=app_private_key,
                now=now,
            ),
        )
        deps = source_deps(
            server=mirror.server, upstream=upstream, sessionmaker=sessionmaker, edit=edit, now=now
        )
        if await check_mirror(deps, mirror.id) == "failed":
            failed += 1
    log.info("skill_repo.resynced", repo=repo_full_name, mirrors=len(mirrors), failed=failed)
    return failed, failed
