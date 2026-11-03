"""`/api/agents/{id}/knowledge`: what an agent remembers, read-only."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_knowledge
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_knowledge import KnowledgeStores
from fastapi import APIRouter
from pydantic import BaseModel


class MemoryText(BaseModel):
    path: str
    content: str


def agent_knowledge_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.get("/{agent_id}/knowledge")
    async def stores(caller: Viewer, agent_id: str) -> KnowledgeStores:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_knowledge.list_stores(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/{agent_id}/knowledge/paths")
    async def paths(caller: Viewer, agent_id: str, store: str) -> list[str]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_knowledge.list_paths(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id, store=store
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/{agent_id}/knowledge/content")
    async def content(caller: Viewer, agent_id: str, store: str, path: str) -> MemoryText:  # pyright: ignore[reportUnusedFunction]
        try:
            text = await agent_knowledge.read(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                store=store,
                path=path,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return MemoryText(path=path, content=text)

    return router
