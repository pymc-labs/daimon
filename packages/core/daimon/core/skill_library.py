"""The workspace's skill library as an admin manages it: list, versions, sync, delete."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from typing import Literal

import anthropic
import httpx
from anthropic.types.beta import SkillListResponse
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_skill_files import MAX_EDIT_BYTES, download_files
from daimon.core.agent_target import ChangeRecorder
from daimon.core.defaults.loader import parse_skill_markdown
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_lenient
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.defaults.report import Action
from daimon.core.errors import DaimonError
from daimon.core.mux_compat import list_skill_versions
from daimon.core.skills.attachments import delete_skill_unless_attached
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores import web_audit
from daimon.core.stores.channel_skills import list_tenant_channel_skills
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.seeded_skills import list_seeded_skill_names
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from daimon.core.stores.web_people import get_person_for_account
from mux.contracts.errors import ProviderError, ScopeViolation
from pydantic import BaseModel

SAVED_ACTION = "Saved skill version"
RESTORED_ACTION = "Restored skill version"
CREATED_ACTION = "Created skill"
_AUTHOR_ROWS = 500


class SkillUse(BaseModel):
    """Where a skill is in use: an agent that pins it, or a channel that adds it."""

    kind: Literal["agent", "channel"]
    id: str
    """The agent's id, or the channel's id on its platform."""
    name: str
    version: str | None
    """The version pinned there, or None for the latest."""


class LibrarySkill(BaseModel):
    id: str
    name: str
    title: str
    """`name` as words, for headings."""
    description: str
    """From the skill's own SKILL.md; the list only has it for shipped skills."""
    source: str
    """`custom` for the workspace's own, else the provider's name for a built-in skill."""
    version: str
    created_at: str
    updated_at: str
    default: bool
    """A skill the server ships; it can't be deleted here."""
    used_by: list[SkillUse]


class SkillDetail(LibrarySkill):
    source_url: str | None
    """Where a GitHub-synced skill lives, else None."""


class SkillVersionDoc(BaseModel):
    version: str
    created_at: str
    author: str | None = None
    """Who saved it from the web app, when that is on record."""


class SyncedSkill(BaseModel):
    name: str
    action: Literal["created", "updated", "skipped", "failed"]
    detail: str | None = None


def own_skills(
    actor: AgentActor, rows: list[SkillListResponse]
) -> list[tuple[SkillListResponse, str]]:
    """Anthropic's skills and this workspace's own by bare name; others are left out."""
    out: list[tuple[SkillListResponse, str]] = []
    for row in rows:
        if row.display_title is None:
            continue
        if row.source == "custom":
            bare = strip_tenant_prefix(tenant_id=actor.tenant_id, display_title=row.display_title)
            if bare is not None:
                out.append((row, bare))
        else:
            out.append((row, row.display_title))
    return out


def title_of(name: str) -> str:
    """`data-validation` as `Data validation`."""
    words = name.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


async def _library(
    deps: AgentEditDeps, actor: AgentActor, descriptions: Mapping[str, str]
) -> list[LibrarySkill]:
    rows, _truncated = await list_skills_lenient(deps.client)
    agents = await list_agents_by_tenant(deps.client, tenant_id=actor.tenant_id)
    uses: dict[str, list[SkillUse]] = defaultdict(list)
    for agent in agents:
        for pinned in agent.skills:
            pin = pinned.version if pinned.version not in (None, "", "latest") else None
            uses[pinned.skill_id].append(
                SkillUse(kind="agent", id=agent.id, name=agent.name, version=pin)
            )
    async with deps.session_factory() as session:
        seeded = await list_seeded_skill_names(session, tenant_id=actor.tenant_id)
        rules = await list_tenant_channel_skills(session, tenant_id=actor.tenant_id)
        names: dict[tuple[str, str], str] = {}
        for platform in {r.platform for r in rules}:
            found = await get_channel_names(
                session,
                tenant_id=actor.tenant_id,
                platform=platform,
                channel_ids={r.channel_id for r in rules if r.platform == platform},
            )
            names.update({(platform, cid): n for cid, n in found.items()})
    for rule in rules:
        label = names.get((rule.platform, rule.channel_id), "Deleted channel")
        uses[rule.skill_id].append(
            SkillUse(kind="channel", id=rule.channel_id, name=label, version=rule.version or None)
        )
    return sorted(
        (
            LibrarySkill(
                id=row.id,
                name=name,
                title=title_of(name),
                description=descriptions.get(name, ""),
                source=row.source,
                version=row.latest_version or "",
                created_at=row.created_at,
                updated_at=row.updated_at,
                default=name in seeded,
                used_by=sorted(uses.get(row.id, []), key=lambda u: (u.kind, u.name.casefold())),
            )
            for row, name in own_skills(actor, rows)
        ),
        key=lambda s: (s.source, s.name.casefold()),
    )


async def list_library(
    deps: AgentEditDeps, *, actor: AgentActor, descriptions: Mapping[str, str]
) -> list[LibrarySkill]:
    """The library with what uses each skill, for an admin."""
    require_admin(actor, "The skill library")
    return await _library(deps, actor, descriptions)


def github_url_of(url: str, branch: str, path: str) -> str | None:
    """The browsable address of a skill synced from GitHub, else None."""
    if not url.startswith("https://github.com/"):
        return None
    base = url.removesuffix(".git").rstrip("/")
    tail = "/".join(part for part in (branch.strip("/"), path.strip("/")) if part)
    return f"{base}/tree/{tail}" if tail else base


async def get_library_skill(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    descriptions: Mapping[str, str],
) -> SkillDetail:
    """One library skill with its own description, read from its SKILL.md when it is custom."""
    require_admin(actor, "The skill library")
    found = next((s for s in await _library(deps, actor, descriptions) if s.id == skill_id), None)
    if found is None:
        raise AgentEditError("No such skill.", kind="not_found")
    description = found.description
    if found.source == "custom" and found.version:
        try:
            files = await download_files(
                deps, actor=actor, skill_id=skill_id, version=found.version
            )
            spec, _body = parse_skill_markdown(
                files["SKILL.md"][:MAX_EDIT_BYTES].decode("utf-8"), source="SKILL.md"
            )
            description = spec.description.strip()
        except (AgentEditError, UnicodeDecodeError, DaimonError):
            # A skill whose SKILL.md can't be read still has a page: its uses and versions.
            pass
    async with deps.session_factory() as session:
        synced = [
            r
            for r in await list_user_skills_for_tenant(session, tenant_id=actor.tenant_id)
            if r.anthropic_id == skill_id and r.source_repo_url
        ]
    source_url = (
        github_url_of(
            synced[0].source_repo_url, synced[0].source_repo_branch, synced[0].source_path
        )
        if synced
        else None
    )
    return SkillDetail(
        **found.model_copy(update={"description": description}).model_dump(),
        source_url=source_url,
    )


async def library_skill(
    deps: AgentEditDeps, actor: AgentActor, skill_id: str
) -> tuple[SkillListResponse, str]:
    rows, _truncated = await list_skills_lenient(deps.client)
    for row, name in own_skills(actor, rows):
        if row.id == skill_id:
            return row, name
    raise AgentEditError("No such skill.", kind="not_found")


async def version_authors(
    deps: AgentEditDeps, *, tenant_id: uuid.UUID, name: str
) -> dict[str, str]:
    """Who saved each version of skill ``name`` from the web app, from the audit trail.

    The provider records no author, and a version saved elsewhere has none here.
    """
    authors: dict[str, str] = {}
    async with deps.session_factory() as session:
        for action in (SAVED_ACTION, RESTORED_ACTION, CREATED_ACTION):
            rows = await web_audit.list_recent(
                session, tenant_id=tenant_id, action=action, limit=_AUTHOR_ROWS
            )
            for row in rows:
                head, _, version = row.target.rpartition(" v")
                if head != name or row.actor_account_id is None:
                    continue
                person = await get_person_for_account(
                    session, tenant_id=tenant_id, account_id=row.actor_account_id
                )
                if person is not None:
                    authors.setdefault(version, person.display_name)
    return authors


async def skill_versions(
    deps: AgentEditDeps, *, actor: AgentActor, skill_id: str
) -> list[SkillVersionDoc]:
    """Every version of a library skill, newest first, with who saved it when known."""
    require_admin(actor, "The skill library")
    _row, name = await library_skill(deps, actor, skill_id)
    authors = await version_authors(deps, tenant_id=actor.tenant_id, name=name)
    try:
        found = [
            SkillVersionDoc(
                version=v.version, created_at=v.created_at, author=authors.get(v.version)
            )
            async for v in list_skill_versions(
                deps.client, skill_id, limit=100, scope=actor.scope()
            )
        ]
    except (anthropic.APIStatusError, ProviderError, ScopeViolation) as exc:
        raise AgentEditError("The versions could not be read. Try again.", kind="conflict") from exc
    return sorted(found, key=lambda v: v.created_at, reverse=True)


async def sync_from_github(
    deps: AgentEditDeps,
    http: httpx.AsyncClient,
    *,
    actor: AgentActor,
    url: str,
    branch: str,
    path: str,
    max_tarball_bytes: int,
    max_tarball_decompressed_bytes: int,
    audited: ChangeRecorder | None = None,
) -> list[SyncedSkill]:
    """Import or update every skill a GitHub repository holds, for an admin.

    Only a repository anyone can read: a private one is read through an agent's
    own GitHub grant, which is made from a chat.
    """
    require_admin(actor, "Syncing skills")
    async with deps.session_factory() as session:
        seeded = await list_seeded_skill_names(session, tenant_id=actor.tenant_id)
    try:
        outcomes = await run_skill_sync(
            deps.client,
            http,
            url=url.strip(),
            branch=branch.strip() or "main",
            path=path.strip().strip("/"),
            tenant_id=actor.tenant_id,
            seeded_skill_names=seeded,
            is_admin=True,
            token=None,
            max_tarball_bytes=max_tarball_bytes,
            max_tarball_decompressed_bytes=max_tarball_decompressed_bytes,
        )
    except GitHubFetchError as exc:
        if exc.status_code == 404:
            raise AgentEditError(
                "That repository could not be read. It may be private or the address wrong.",
                kind="invalid",
            ) from exc
        raise AgentEditError(str(exc), kind="invalid") from exc
    except DaimonError as exc:
        raise AgentEditError(str(exc), kind="invalid") from exc
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, url.strip())
    return [
        SyncedSkill(
            name=o.name,
            action="failed"
            if o.error
            else "skipped"
            if o.refusal or o.action not in (Action.CREATED, Action.UPDATED)
            else "created"
            if o.action == Action.CREATED
            else "updated",
            detail=o.refusal or o.error,
        )
        for o in outcomes
    ]


async def delete_library_skill(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    audited: ChangeRecorder | None = None,
) -> str:
    """Delete a library skill and every version, unless an agent or channel still uses it."""
    require_admin(actor, "Deleting a skill")
    row, name = await library_skill(deps, actor, skill_id)
    if row.source != "custom":
        raise AgentEditError("A built-in skill can't be deleted.", kind="forbidden")
    async with deps.session_factory() as session:
        seeded = await list_seeded_skill_names(session, tenant_id=actor.tenant_id)
    if name in seeded:
        raise AgentEditError(f"{name} is a default skill and can't be deleted.", kind="forbidden")
    async with deps.session_factory() as session, session.begin():
        outcome = await delete_skill_unless_attached(
            deps.client, session, skill_id=skill_id, detach=False, scope=actor.scope()
        )
        if not outcome.deleted:
            mine = str(actor.tenant_id)
            raise AgentEditError(
                f"{name} is still used by "
                + _places(
                    [a.name for a in outcome.attachments.agents if a.tenant == mine],
                    len(outcome.attachments.channels),
                )
                + ". Remove it there first.",
                kind="conflict",
            )
        if audited is not None:
            await audited(session, name)
    return name


def _places(agents: list[str], channels: int) -> str:
    parts = [", ".join(sorted(set(agents)))] if agents else []
    if channels:
        parts.append(f"{channels} channel{'s' if channels != 1 else ''}")
    return " and ".join(parts)


__all__ = [
    "CREATED_ACTION",
    "RESTORED_ACTION",
    "SAVED_ACTION",
    "LibrarySkill",
    "SkillDetail",
    "SkillUse",
    "SkillVersionDoc",
    "SyncedSkill",
    "delete_library_skill",
    "get_library_skill",
    "github_url_of",
    "library_skill",
    "list_library",
    "own_skills",
    "title_of",
    "version_authors",
    "skill_versions",
    "sync_from_github",
]
