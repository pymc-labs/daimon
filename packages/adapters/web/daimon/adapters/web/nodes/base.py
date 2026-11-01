"""Connection nodes: servers an agent is linked to from the agent editor.

Each kind claims the agent MCP servers that point at it, says whether it can be
added in a workspace, and serves its own routes under
`/api/agents/{agent_id}/nodes/{kind}`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from fastapi import APIRouter
from pydantic import BaseModel


class AgentNode(BaseModel):
    """A node kind on an agent: `server` is the MCP server key it claims, None if addable."""

    kind: str
    label: str
    server: str | None


class ConnectionNode(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def label(self) -> str: ...

    def claims(self, server_url: str) -> bool:
        """Whether an agent MCP server at `server_url` is this node."""
        ...

    def offered(self, workspace: str | None) -> bool:
        """Whether agents of `workspace` (`<platform>:<id>`) may add this node."""
        ...

    async def status(self, runtime: WebRuntime, caller: WebCaller, agent_id: str) -> BaseModel:
        """What the node knows of the agent, as the caller."""
        ...

    def router(self, runtime: WebRuntime) -> APIRouter:
        """The kind's own routes, relative to the node's path."""
        ...


def agent_nodes(
    nodes: Iterable[ConnectionNode], *, server_urls: Mapping[str, str], workspace: str | None
) -> list[AgentNode]:
    """Each node the agent has, by the server key it claims, then each it can add. Pure."""
    found: list[AgentNode] = []
    for node in nodes:
        key = next((k for k, url in server_urls.items() if node.claims(url)), None)
        if key is not None or node.offered(workspace):
            found.append(AgentNode(kind=node.kind, label=node.label, server=key))
    return found
