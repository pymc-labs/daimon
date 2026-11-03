"""`/api/agents/{id}/archive` and `/copy`: retiring or copying an agent, for admins."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Editor, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_admin
from daimon.core.agent_editing import AgentEditError
from fastapi import APIRouter
from pydantic import BaseModel


class CopyBody(BaseModel):
    name: str


class CopyDoc(BaseModel):
    id: str
    name: str


def agent_lifecycle_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.post("/{agent_id}/archive", status_code=204)
    async def archive(caller: Editor, agent_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_admin.archive(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                audited=recorded(runtime, caller, "Archived agent"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)

    @router.post("/{agent_id}/copy")
    async def copy(caller: Editor, agent_id: str, body: CopyBody) -> CopyDoc:  # pyright: ignore[reportUnusedFunction]
        try:
            made = await agent_admin.copy(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                new_name=body.name.strip(),
                audited=recorded(runtime, caller, "Copied agent"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        return CopyDoc(id=made.agent.id, name=body.name.strip())

    return router
