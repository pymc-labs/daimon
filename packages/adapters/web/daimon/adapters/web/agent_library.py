"""`/api/agents/skills` and `/api/agents/environments`: the skill library and sandboxes."""

from __future__ import annotations

from typing import Literal

from daimon.adapters.web.agent_access import Editor, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.channel_names import channel_names
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import environment_admin, skill_library, skill_library_files
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_skill_files import SkillFilesDoc
from daimon.core.environment_admin import EnvironmentDoc, EnvironmentInput, EnvironmentNeedsConfirm
from daimon.core.skill_library import (
    CREATED_ACTION,
    RESTORED_ACTION,
    SAVED_ACTION,
    LibrarySkill,
    SkillDetail,
    SkillVersionDoc,
    SyncedSkill,
)
from daimon.core.skill_library_files import SkillRef
from daimon.core.stores.scoped_config_read import list_environment_picks
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class SyncBody(BaseModel):
    url: str
    branch: str = "main"
    path: str = ""


class NewSkillBody(BaseModel):
    name: str
    description: str


class SaveFilesBody(BaseModel):
    files: dict[str, str | None]
    """A path with null content keeps its bytes from ``expected_version``."""
    expected_version: str


class RestoreBody(BaseModel):
    version: str


class EnvironmentBody(BaseModel):
    spec: EnvironmentInput
    confirm_open_network: bool = False
    expected_updated_at: str | None = None


class EnvironmentUse(BaseModel):
    kind: Literal["channel", "workspace"]
    id: str
    name: str


class EnvironmentListing(EnvironmentDoc):
    used_by: list[EnvironmentUse]
    """The channels and the workspace default that pick it."""


class ArchivedDoc(BaseModel):
    note: str


def _needs_confirm(exc: EnvironmentNeedsConfirm) -> HTTPException:
    return HTTPException(status_code=428, detail=str(exc))


def agent_library_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.get("/skills")
    async def skills(caller: Editor) -> list[LibrarySkill]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await skill_library.list_library(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                descriptions=runtime.skill_descriptions,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/skills", status_code=201)
    async def create_skill(caller: Editor, body: NewSkillBody) -> SkillRef:  # pyright: ignore[reportUnusedFunction]
        try:
            return await skill_library_files.create_library_skill(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                name=body.name,
                description=body.description,
                audited=recorded(runtime, caller, CREATED_ACTION),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/skills/{skill_id}")
    async def skill(caller: Editor, skill_id: str) -> SkillDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            return await skill_library.get_library_skill(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                descriptions=runtime.skill_descriptions,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/skills/{skill_id}/files")
    async def skill_files(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, skill_id: str, version: str | None = None
    ) -> SkillFilesDoc:
        try:
            return await skill_library_files.read_library_files(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                version=version,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.put("/skills/{skill_id}/files")
    async def save_skill_files(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, skill_id: str, body: SaveFilesBody
    ) -> SkillRef:
        try:
            return await skill_library_files.save_library_files(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                files=body.files,
                expected_version=body.expected_version,
                audited=recorded(runtime, caller, SAVED_ACTION),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/skills/{skill_id}/restore")
    async def restore_skill(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, skill_id: str, body: RestoreBody
    ) -> SkillRef:
        try:
            return await skill_library_files.restore_library_version(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                version=body.version,
                audited=recorded(runtime, caller, RESTORED_ACTION),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/skills/{skill_id}/versions")
    async def versions(caller: Editor, skill_id: str) -> list[SkillVersionDoc]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await skill_library.skill_versions(
                change_deps(runtime).edit, actor=actor_of(caller), skill_id=skill_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/skills/sync")
    async def sync(caller: Editor, body: SyncBody) -> list[SyncedSkill]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await skill_library.sync_from_github(
                change_deps(runtime).edit,
                runtime.http_client,
                actor=actor_of(caller),
                url=body.url,
                branch=body.branch,
                path=body.path,
                max_tarball_bytes=runtime.settings.github.max_tarball_bytes,
                max_tarball_decompressed_bytes=runtime.settings.github.max_tarball_decompressed_bytes,
                audited=recorded(runtime, caller, "Synced skills"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.delete("/skills/{skill_id}", status_code=204)
    async def delete_skill(caller: Editor, skill_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await skill_library.delete_library_skill(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                audited=recorded(runtime, caller, "Deleted skill"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/environments")
    async def environments(caller: Editor) -> list[EnvironmentListing]:  # pyright: ignore[reportUnusedFunction]
        try:
            docs = await environment_admin.list_environments(
                change_deps(runtime).edit, actor=actor_of(caller)
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        async with runtime.sessionmaker() as db:
            picks = await list_environment_picks(db, tenant_id=caller.person.tenant_id)
        _, found = await channel_names(runtime, caller.person.tenant_id, {c for _, c in picks if c})
        labels = found.labels()
        uses: dict[str, list[EnvironmentUse]] = {}
        for env_name, channel_id in picks:
            use = (
                EnvironmentUse(kind="workspace", id="workspace", name="Workspace default")
                if channel_id is None
                else EnvironmentUse(
                    kind="channel", id=channel_id, name=labels.get(channel_id, "Deleted channel")
                )
            )
            uses.setdefault(env_name, []).append(use)
        return [
            EnvironmentListing(**doc.model_dump(), used_by=uses.get(doc.name, [])) for doc in docs
        ]

    @router.post("/environments")
    async def create_environment(caller: Editor, body: EnvironmentInput) -> EnvironmentDoc:  # pyright: ignore[reportUnusedFunction]
        try:
            return await environment_admin.create(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                spec=body,
                audited=recorded(runtime, caller, "Created environment"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.put("/environments/{environment_id}")
    async def update_environment(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, environment_id: str, body: EnvironmentBody
    ) -> EnvironmentDoc:
        try:
            return await environment_admin.update(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                environment_id=environment_id,
                spec=body.spec,
                confirmed=body.confirm_open_network,
                expected_updated_at=body.expected_updated_at,
                audited=recorded(runtime, caller, "Changed environment"),
            )
        except EnvironmentNeedsConfirm as exc:
            raise _needs_confirm(exc) from exc
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.delete("/environments/{environment_id}")
    async def archive_environment(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, environment_id: str, confirm_open_network: bool = False
    ) -> ArchivedDoc:
        try:
            note = await environment_admin.archive(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                environment_id=environment_id,
                confirmed=confirm_open_network,
                audited=recorded(runtime, caller, "Archived environment"),
            )
        except EnvironmentNeedsConfirm as exc:
            raise _needs_confirm(exc) from exc
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return ArchivedDoc(note=note)

    return router
