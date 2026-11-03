"""`/api/agents/{id}/google`: the Google Workspace identity bound to an agent, read-only."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_google
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_google import GoogleBinding
from fastapi import APIRouter


def agent_google_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.get("/{agent_id}/google")
    async def binding(caller: Viewer, agent_id: str) -> GoogleBinding | None:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_google.get_binding(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router
