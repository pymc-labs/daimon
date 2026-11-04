"""Read, save, create and restore a library skill's files, for an admin.

The library's own skills are the custom ones titled `{tenant}-{name}`. A version never
changes, so every save publishes a new one; a save names the version the editor loaded and
is refused when another has been published since. Each publish is recorded with the saving
account, which is where a version's author comes from: the provider records none.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anthropic
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_skill_files import (
    MAX_EDIT_BYTES,
    SkillFilesDoc,
    concrete_version,
    download_files,
    files_doc,
)
from daimon.core.agent_target import ChangeRecorder
from daimon.core.defaults.ma_index import list_skills_lenient
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.mux_compat import create_skill, publish_skill_version
from daimon.core.skill_library import library_skill, own_skills
from daimon.core.skills.ingest import SkillIngestError, bundle_from_files
from daimon.core.stores.server_skills import mirror_of_skill
from mux.contracts.errors import ProviderError, ScopeViolation
from pydantic import BaseModel
from sqlalchemy import text

__all__ = [
    "SkillRef",
    "create_custom_skill",
    "create_library_skill",
    "custom_skill_names",
    "fork_library_skill",
    "read_library_files",
    "restore_library_version",
    "save_library_files",
    "skill_lock",
]

_NAME = re.compile(r"[^a-z0-9]+")


class SkillRef(BaseModel):
    id: str
    name: str
    version: str


def slug_of(text: str) -> str:
    """`Data validation` as `data-validation`."""
    return _NAME.sub("-", text.strip().lower()).strip("-")


_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


@asynccontextmanager
async def skill_lock(
    deps: AgentEditDeps, *, tenant_id: uuid.UUID, skill_id: str
) -> AsyncIterator[None]:
    """Serialize publishing versions of one skill, until the block ends.

    The version check and the publish are separate provider calls; held across both, a
    second save sees the first's version and is refused as stale rather than overwriting it.
    """
    key = f"library_skill:{tenant_id}:{skill_id}"
    # Advisory locks are held per connection, so two saves that share one connection both
    # pass; the process lock orders them first, the advisory lock orders other processes.
    local = _LOCKS.get(key) or _LOCKS.setdefault(key, asyncio.Lock())
    async with local, deps.session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        yield


def _target(name: str, version: str) -> str:
    return f"{name} v{version}"


async def _custom(
    deps: AgentEditDeps, actor: AgentActor, skill_id: str, *, verb: str
) -> tuple[str, str]:
    """The skill's name and latest version, if it is one the workspace can change."""
    require_admin(actor, "The skill library")
    row, name = await library_skill(deps, actor, skill_id)
    if row.source != "custom":
        raise AgentEditError(f"A built-in skill can't be {verb}.", kind="forbidden")
    async with deps.session_factory() as session:
        mirror = await mirror_of_skill(session, tenant_id=actor.tenant_id, skill_id=skill_id)
    if mirror is not None:
        raise AgentEditError(f"A server skill can't be {verb}. Fork it first.", kind="forbidden")
    return name, row.latest_version or ""


async def read_library_files(
    deps: AgentEditDeps, *, actor: AgentActor, skill_id: str, version: str | None = None
) -> SkillFilesDoc:
    """The files of one version of a library skill, the latest by default."""
    require_admin(actor, "The skill library")
    row, _name = await library_skill(deps, actor, skill_id)
    held = await concrete_version(
        deps, actor=actor, skill_id=skill_id, version=version or row.latest_version or "latest"
    )
    files = await download_files(deps, actor=actor, skill_id=skill_id, version=held)
    return files_doc(skill_id, held, files)


async def _publish(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    name: str,
    bodies: dict[str, bytes],
    audited: ChangeRecorder | None,
) -> SkillRef:
    try:
        bundle = bundle_from_files(bodies)
    except SkillIngestError as exc:
        raise AgentEditError(str(exc), kind="invalid") from exc
    if bundle.preview.name != name:
        raise AgentEditError(f"SKILL.md name must stay {name}.", kind="invalid")
    try:
        published = await publish_skill_version(
            deps.client, skill_id, data=bundle.zip_bytes, scope=actor.scope()
        )
    except (anthropic.APIStatusError, ProviderError, ScopeViolation) as exc:
        raise AgentEditError(
            "The skill could not be saved. Try again.", kind="unavailable"
        ) from exc
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, _target(name, published.version))
    return SkillRef(id=skill_id, name=name, version=published.version)


async def save_library_files(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    files: dict[str, str | None],
    expected_version: str,
    audited: ChangeRecorder | None = None,
) -> SkillRef:
    """Publish ``files`` as the skill's next version.

    A file with None content keeps its bytes from ``expected_version``. A later version than
    that is a conflict: the editor reloads rather than overwrite it.
    """
    require_admin(actor, "The skill library")
    async with skill_lock(deps, tenant_id=actor.tenant_id, skill_id=skill_id):
        name, latest = await _custom(deps, actor, skill_id, verb="edited")
        if latest != expected_version:
            raise AgentEditError(
                "This skill changed since you opened it. Reload to see the new version.",
                kind="stale",
            )
        bodies: dict[str, bytes] = {
            path: body.encode("utf-8") for path, body in files.items() if body is not None
        }
        too_big = sorted(path for path, body in bodies.items() if len(body) > MAX_EDIT_BYTES)
        if too_big:
            raise AgentEditError(
                f"{too_big[0]} is over {MAX_EDIT_BYTES} bytes, the most the editor saves.",
                kind="invalid",
            )
        kept = {path for path, body in files.items() if body is None}
        if kept:
            original = await download_files(
                deps, actor=actor, skill_id=skill_id, version=expected_version
            )
            if not kept <= original.keys():
                raise AgentEditError("A kept file is not in the skill.", kind="invalid")
            bodies |= {path: original[path] for path in kept}
        return await _publish(
            deps,
            actor=actor,
            skill_id=skill_id,
            name=name,
            bodies=bodies,
            audited=audited,
        )


async def restore_library_version(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    version: str,
    audited: ChangeRecorder | None = None,
) -> SkillRef:
    """Publish an older version's files again as the next version."""
    require_admin(actor, "The skill library")
    async with skill_lock(deps, tenant_id=actor.tenant_id, skill_id=skill_id):
        name, _latest = await _custom(deps, actor, skill_id, verb="edited")
        old = await download_files(deps, actor=actor, skill_id=skill_id, version=version)
        return await _publish(
            deps,
            actor=actor,
            skill_id=skill_id,
            name=name,
            bodies=old,
            audited=audited,
        )


async def create_library_skill(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    name: str,
    description: str,
    audited: ChangeRecorder | None = None,
) -> SkillRef:
    """A new custom skill holding a SKILL.md with this name and description."""
    require_admin(actor, "The skill library")
    slug = slug_of(name)
    summary = " ".join(description.split())
    skill_md = f"---\nname: {slug}\ndescription: {json.dumps(summary)}\n---\n\n# {name.strip()}\n"
    ref = await create_custom_skill(deps, actor=actor, files={"SKILL.md": skill_md.encode("utf-8")})
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, _target(ref.name, ref.version))
    return ref


async def custom_skill_names(deps: AgentEditDeps, *, actor: AgentActor) -> frozenset[str]:
    """The names the tenant's own library skills hold."""
    rows, _truncated = await list_skills_lenient(deps.client)
    return frozenset(bare for row, bare in own_skills(actor, rows) if row.source == "custom")


async def create_custom_skill(
    deps: AgentEditDeps, *, actor: AgentActor, files: dict[str, bytes]
) -> SkillRef:
    """A new library skill holding `files`, named by their SKILL.md, after every ingest check."""
    try:
        bundle = bundle_from_files(files)
    except SkillIngestError as exc:
        raise AgentEditError(str(exc), kind="invalid") from exc
    slug = bundle.preview.name
    if slug in await custom_skill_names(deps, actor=actor):
        raise AgentEditError(f"A skill named {slug} already exists.", kind="taken")
    title = tenant_scoped_display_title(tenant_id=actor.tenant_id, name=slug)
    try:
        created = await create_skill(
            deps.client, display_title=title, data=bundle.zip_bytes, scope=actor.scope()
        )
    except (anthropic.APIStatusError, ProviderError, ScopeViolation) as exc:
        raise AgentEditError(
            "The skill could not be created. Try again.", kind="unavailable"
        ) from exc
    version = created.latest_version or ""
    if version in ("", "latest"):
        raise AgentEditError("The skill could not be created. Try again.", kind="unavailable")
    return SkillRef(id=created.id, name=slug, version=version)


_FRONT = re.compile(r"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_FRONT_NAME = re.compile(r"^name:[^\r\n]*", re.MULTILINE)


def _renamed(skill_md: str, slug: str) -> str | None:
    """SKILL.md with its frontmatter `name:` set to `slug`; None without one."""
    front = _FRONT.match(skill_md)
    if front is None:
        return None
    head, count = _FRONT_NAME.subn(f"name: {slug}", front.group(0), count=1)
    return None if count != 1 else head + skill_md[front.end() :]


async def fork_library_skill(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    name: str,
    audited: ChangeRecorder | None = None,
) -> SkillRef:
    """An editable copy of a server skill's latest version, under `name`; it follows nothing."""
    require_admin(actor, "The skill library")
    row, _old = await library_skill(deps, actor, skill_id)
    slug = slug_of(name)
    if not slug:
        raise AgentEditError("Give the copy a name.", kind="invalid")
    files = await download_files(
        deps, actor=actor, skill_id=skill_id, version=row.latest_version or ""
    )
    skill_md = files.get("SKILL.md", b"").decode("utf-8", errors="replace")
    renamed = _renamed(skill_md, slug)
    if renamed is None:
        raise AgentEditError("This skill has no name to change.", kind="invalid")
    ref = await create_custom_skill(
        deps, actor=actor, files={**files, "SKILL.md": renamed.encode("utf-8")}
    )
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, _target(ref.name, ref.version))
    return ref
