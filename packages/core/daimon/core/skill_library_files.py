"""Read, save, create and restore a library skill's files, for an admin.

The library's own skills are the custom ones titled `{tenant}-{name}`. A version never
changes, so every save publishes a new one; a save names the version the editor loaded and
is refused when another has been published since. Each publish is recorded with the saving
account, which is where a version's author comes from: the provider records none.
"""

from __future__ import annotations

import json
import re

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
from mux.contracts.errors import ProviderError, ScopeViolation
from pydantic import BaseModel

__all__ = [
    "SkillRef",
    "create_library_skill",
    "read_library_files",
    "restore_library_version",
    "save_library_files",
]

_NAME = re.compile(r"[^a-z0-9]+")


class SkillRef(BaseModel):
    id: str
    name: str
    version: str


def slug_of(text: str) -> str:
    """`Data validation` as `data-validation`."""
    return _NAME.sub("-", text.strip().lower()).strip("-")


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
        raise AgentEditError("The skill could not be saved. Try again.", kind="conflict") from exc
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
    try:
        bundle = bundle_from_files({"SKILL.md": skill_md.encode("utf-8")})
    except SkillIngestError as exc:
        raise AgentEditError(str(exc), kind="invalid") from exc
    rows, _truncated = await list_skills_lenient(deps.client)
    if any(bare == slug and row.source == "custom" for row, bare in own_skills(actor, rows)):
        raise AgentEditError(f"A skill named {slug} already exists.", kind="taken")
    title = tenant_scoped_display_title(tenant_id=actor.tenant_id, name=slug)
    try:
        created = await create_skill(
            deps.client, display_title=title, data=bundle.zip_bytes, scope=actor.scope()
        )
    except (anthropic.APIStatusError, ProviderError, ScopeViolation) as exc:
        raise AgentEditError("The skill could not be created. Try again.", kind="conflict") from exc
    version = created.latest_version or ""
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, _target(slug, version))
    return SkillRef(id=created.id, name=slug, version=version)
