"""`/api/agents/skills` and `/api/agents/environments`: the skill library and sandboxes."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Editor, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import environment_admin, skill_library
from daimon.core.agent_editing import AgentEditError
from daimon.core.environment_admin import EnvironmentDoc, EnvironmentInput, EnvironmentNeedsConfirm
from daimon.core.skill_library import LibrarySkill, SkillVersionDoc, SyncedSkill
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class SyncBody(BaseModel):
    url: str
    branch: str = "main"
    path: str = ""


class EnvironmentBody(BaseModel):
    spec: EnvironmentInput
    confirm_open_network: bool = False


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
                change_deps(runtime).edit, actor=actor_of(caller)
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
    async def environments(caller: Editor) -> list[EnvironmentDoc]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await environment_admin.list_environments(
                change_deps(runtime).edit, actor=actor_of(caller)
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

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
