"""Read and write the files of an agent's own skills, for the web editor.

A skill is attached by a pinned version, and a version never changes. Editing a file
therefore publishes a new version of the skill (through the same checks and ledger as
adding one) and returns it for the caller's draft to pin; the agent itself is written only
when the draft is applied.
"""

from __future__ import annotations

from typing import Final

import anthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import agent_editing
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_view import AgentConfigDoc, SkillDoc
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mux_compat import download_skill_version
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import (
    SkillIngestError,
    bundle_from_files,
    retained_archive_files,
)
from daimon.core.stores import agent_drafts as drafts_store
from mux.contracts.errors import ScopeViolation
from pydantic import BaseModel, ConfigDict

__all__ = ["SkillFileDoc", "SkillFilesDoc", "read_skill_files", "save_skill_files"]

#: Over this a file is listed but not opened for editing.
MAX_EDIT_BYTES: Final = 200_000


class SkillFileDoc(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    size: int
    """None content means a binary or oversized file: listed, kept as is, not editable."""
    content: str | None


class SkillFilesDoc(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: str
    version: str
    files: list[SkillFileDoc]


def _refused(exc: Exception) -> AgentEditError:
    return AgentEditError(str(exc), kind="invalid")


async def _attached_skill(
    deps: AgentEditDeps, *, actor: AgentActor, agent: BetaManagedAgentsAgent, skill_id: str
) -> str:
    """The version of ``skill_id`` the agent pins, else the one the actor's draft pins."""
    for skill in agent.skills:
        if skill.skill_id == skill_id:
            return skill.version or ""
    async with deps.session_factory() as session:
        row = await drafts_store.get_draft(
            session, tenant_id=actor.tenant_id, agent_id=agent.id, account_id=actor.account_id
        )
    if row is not None:
        for drafted in AgentConfigDoc.model_validate(row.config).skills:
            if drafted.skill_id == skill_id:
                return drafted.version
    raise AgentEditError("This skill is not on the agent.", kind="not_found")


async def read_skill_files(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    skill_id: str,
    version: str | None = None,
) -> SkillFilesDoc:
    """The files of a custom skill the agent (or the actor's draft of it) holds."""
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id=ma_agent_id)
    pinned = await _attached_skill(deps, actor=actor, agent=agent, skill_id=skill_id)
    held = version or pinned
    if not held or held == "latest":
        raise AgentEditError("This skill has no version to read.", kind="invalid")
    try:
        data = await download_skill_version(deps.client, skill_id, held, scope=actor.scope())
        files = retained_archive_files(data)
    except (anthropic.APIStatusError, ScopeViolation, SkillIngestError) as exc:
        raise AgentEditError("This skill's files can't be read.", kind="not_found") from exc
    out: list[SkillFileDoc] = []
    for path in sorted(files, key=lambda p: (p != "SKILL.md", p)):
        body = files[path]
        text: str | None = None
        if len(body) <= MAX_EDIT_BYTES:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        out.append(SkillFileDoc(path=path, size=len(body), content=text))
    return SkillFilesDoc(skill_id=skill_id, version=held, files=out)


async def save_skill_files(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    skill_id: str | None,
    files: dict[str, str | None],
) -> SkillDoc:
    """Publish ``files`` as a new version of the agent's skill, or as a new skill.

    A file with None content keeps the bytes of the version being replaced. The agent is
    not written; the caller pins the returned version in its draft. A SKILL.md that names
    another skill creates that one instead.
    """
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id=ma_agent_id)
    kept = {path for path, body in files.items() if body is None}
    bodies: dict[str, bytes] = {
        path: body.encode("utf-8") for path, body in files.items() if body is not None
    }
    if kept:
        if skill_id is None:
            raise AgentEditError("A new skill has no files to keep.", kind="invalid")
        current = await read_skill_files(
            deps, actor=actor, ma_agent_id=ma_agent_id, skill_id=skill_id
        )
        held = {f.path for f in current.files}
        if not kept <= held:
            raise AgentEditError("A kept file is not in the skill.", kind="invalid")
        data = await download_skill_version(
            deps.client, skill_id, current.version, scope=actor.scope()
        )
        original = retained_archive_files(data)
        bodies |= {path: original[path] for path in kept}
    try:
        bundle = bundle_from_files(bodies)
    except SkillIngestError as exc:
        raise _refused(exc) from exc
    name = agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        await agent_editing.check_may_edit(deps, actor=actor, agent=fresh)

    try:
        added = await add_agent_skill(
            deps.client,
            deps.session_factory,
            tenant_id=actor.tenant_id,
            agent=agent,
            agent_name=name,
            bundle=bundle,
            origin="web",
            added_by_account_id=actor.account_id,
            recheck=recheck,
            attach=False,
        )
    except SkillIngestError as exc:
        raise _refused(exc) from exc
    return SkillDoc(
        type="custom",
        skill_id=added.skill_id,
        title=added.name,
        description=bundle.preview.description,
        version=added.version or "",
    )
