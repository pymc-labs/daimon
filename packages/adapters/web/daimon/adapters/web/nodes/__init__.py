"""Connection nodes and their routes under `/api/agents/{agent_id}/nodes/{kind}`."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Viewer
from daimon.adapters.web.nodes.base import AgentNode, ConnectionNode, agent_nodes
from daimon.adapters.web.nodes.exchange import tenant_workspace
from daimon.adapters.web.nodes.kinds import build_nodes
from daimon.adapters.web.runtime import WebRuntime
from fastapi import APIRouter
from pydantic import JsonValue

__all__ = [
    "AgentNode",
    "ConnectionNode",
    "agent_nodes",
    "build_nodes",
    "nodes_router",
    "tenant_workspace",
]


def _node_router(runtime: WebRuntime, node: ConnectionNode) -> APIRouter:
    router = node.router(runtime)

    @router.get("")
    async def status(caller: Viewer, agent_id: str) -> JsonValue:  # pyright: ignore[reportUnusedFunction]
        found = await node.status(runtime, caller, agent_id)
        return found.model_dump(mode="json")

    return router


def nodes_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()
    for node in build_nodes(runtime.settings):
        router.include_router(
            _node_router(runtime, node), prefix=f"/api/agents/{{agent_id}}/nodes/{node.kind}"
        )
    return router
