"""The workspace's skill library as an admin manages it: list, versions, sync, delete."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

import anthropic
import httpx
from anthropic.types.beta import SkillListResponse
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_lenient
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.defaults.report import Action
from daimon.core.errors import DaimonError
from daimon.core.mux_compat import list_skill_versions
from daimon.core.skills.attachments import delete_skill_unless_attached
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores.channel_skills import list_tenant_channel_skills
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.seeded_skills import list_seeded_skill_names
from mux.contracts.errors import ProviderError, ScopeViolation
from pydantic import BaseModel


class SkillUse(BaseModel):
    """Where a skill is in use: an agent that pins it, or a channel that adds it."""

    kind: Literal["agent", "channel"]
    name: str


class LibrarySkill(BaseModel):
    id: str
    name: str
    source: str
    """`custom` for the workspace's own, else the provider's name for a built-in skill."""
    version: str
    created_at: str
    default: bool
    """A skill the server ships; it can't be deleted here."""
    used_by: list[SkillUse]


class SkillVersionDoc(BaseModel):
    version: str
    created_at: str


class SyncedSkill(BaseModel):
    name: str
    action: Literal["created", "updated", "skipped", "failed"]
    detail: str | None = None


def _own(actor: AgentActor, rows: list[SkillListResponse]) -> list[tuple[SkillListResponse, str]]:
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


async def list_library(deps: AgentEditDeps, *, actor: AgentActor) -> list[LibrarySkill]:
    """The library with what uses each skill, for an admin."""
    require_admin(actor, "The skill library")
    rows, _truncated = await list_skills_lenient(deps.client)
    agents = await list_agents_by_tenant(deps.client, tenant_id=actor.tenant_id)
    uses: dict[str, list[SkillUse]] = defaultdict(list)
    for agent in agents:
        for pinned in agent.skills:
            uses[pinned.skill_id].append(SkillUse(kind="agent", name=agent.name))
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
        label = names.get((rule.platform, rule.channel_id), rule.channel_id)
        uses[rule.skill_id].append(SkillUse(kind="channel", name=label))
    return sorted(
        (
            LibrarySkill(
                id=row.id,
                name=name,
                source=row.source,
                version=row.latest_version or "",
                created_at=row.created_at,
                default=name in seeded,
                used_by=sorted(uses.get(row.id, []), key=lambda u: (u.kind, u.name.casefold())),
            )
            for row, name in _own(actor, rows)
        ),
        key=lambda s: (s.source, s.name.casefold()),
    )


async def _library_skill(
    deps: AgentEditDeps, actor: AgentActor, skill_id: str
) -> tuple[SkillListResponse, str]:
    rows, _truncated = await list_skills_lenient(deps.client)
    for row, name in _own(actor, rows):
        if row.id == skill_id:
            return row, name
    raise AgentEditError("No such skill.", kind="not_found")


async def skill_versions(
    deps: AgentEditDeps, *, actor: AgentActor, skill_id: str
) -> list[SkillVersionDoc]:
    """Every version of a library skill, newest first."""
    require_admin(actor, "The skill library")
    await _library_skill(deps, actor, skill_id)
    try:
        found = [
            SkillVersionDoc(version=v.version, created_at=v.created_at)
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
    row, name = await _library_skill(deps, actor, skill_id)
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
    "LibrarySkill",
    "SkillUse",
    "SkillVersionDoc",
    "SyncedSkill",
    "delete_library_skill",
    "list_library",
    "skill_versions",
    "sync_from_github",
]
