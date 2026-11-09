"""A workspace the node's server doesn't serve sees no node: no label, no node routes."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.nodes.base import agent_nodes
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.adapters.web.static import NOT_FOUND
from daimon.core import node_exchange
from daimon.core.mcp_identity import generate_signing_key
from daimon.core.node_exchange import serves_tenant
from daimon.testing.factories import make_tenant
from daimon.testing.mcp_skills import SkillsServer, skill_md
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_nodes import _BASE, _NODE, _OWN, _RELAY, _client, _settings, _setup
from .test_server_skills_routes import _Node

_LABEL = "Zorblax"
_PATHS = ("/nodes/", "server-skills")
_MODULES = ("daimon.adapters.web.nodes", "daimon.adapters.web.server_skills")

# Routes of a node module that name no node and reach no server, with the reason.
NODE_FREE = {
    ("POST", "/api/agents/skills/{skill_id}/fork"): "copies a library skill",
    ("GET", "/api/agents/{agent_id}/skill-follows"): "reads the agent's own follow choices",
    ("PUT", "/api/agents/{agent_id}/skill-follows"): "writes them; fetches only when served",
    ("POST", "/api/agents/{agent_id}/skill-follows/{skill_id}/apply"): "applies a held update",
}

_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/api/agents/server-skills"): {"uri": "x"},
    ("POST", "/api/agents/skills/{skill_id}/approve"): {"digest": "d"},
    ("PUT", "/api/agents/{agent_id}/nodes/data_server/tools"): {
        "connection_id": "c",
        "allow": True,
    },
}


def _node_routes(app: Any) -> list[APIRoute]:
    """Every route the node's two modules serve, found from the built app."""
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    by_module = [r for r in routes if r.endpoint.__module__.startswith(_MODULES)]
    by_path = [r for r in routes if any(p in r.path for p in _PATHS)]
    assert {r.path for r in by_path} <= {r.path for r in by_module}, (
        "a node or server skill path is served from outside the modules this test knows"
    )
    return by_module


def _fill(path: str) -> str:
    return (
        path.replace("{agent_id}", "ag_w")
        .replace("{skill_id}", "sk_1")
        .replace("{token_id}", str(uuid.uuid4()))
    )


async def test_every_node_route_is_not_found_for_an_unserved_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Routes come from the app; each gets 404 before any exchange, naming nothing."""
    agents, owner, _ = await _setup(db_session)
    key = generate_signing_key()
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    settings = _settings(key, workspaces=["discord:x"], node={**_NODE, "label": _LABEL})
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        assert isinstance(transport, httpx.ASGITransport)
        routes = _node_routes(transport.app)
        covered = 0
        for route in routes:
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                key_ = (method, route.path)
                if key_ in NODE_FREE:
                    continue
                res = await client.request(
                    method, _fill(route.path), json=_BODIES.get(key_, {}), headers=_OWN
                )
                assert (res.status_code, res.json()) == (404, {"detail": NOT_FOUND}), (
                    f"{key_} answered {res.status_code} {res.text}"
                )
                assert _LABEL not in res.text, f"{key_} names the node"
                covered += 1
    assert covered >= 10, "the node's routes were found"
    paths = {(m, r.path) for r in routes for m in r.methods}
    assert {
        ("POST", "/api/agents/skills/{skill_id}/check"),
        ("POST", "/api/agents/skills/{skill_id}/approve"),
    } <= paths - set(NODE_FREE), "check and approve are covered, not allowed through"
    assert node.subject_tokens == [] and node.skills.requests == [], "the server is never asked"
    assert set(NODE_FREE) <= paths, "every allowed route still exists"


async def test_node_routes_still_answer_a_workspace_the_server_serves(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The gate is the workspace list, not the node: a listed workspace gets the node."""
    agents, owner, _ = await _setup(db_session)
    key = generate_signing_key()
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    settings = _settings(key, workspaces=["discord:g-1"], node={**_NODE, "label": _LABEL})
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        status = await client.get("/api/agents/ag_w/nodes/data_server")
        menu = await client.get("/api/agents/server-skills")
    assert status.status_code == 200, status.text
    assert menu.json()["label"] == _LABEL, menu.text


async def test_an_agent_claiming_the_node_by_url_still_lists_no_node_when_unserved(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The agent's MCP server URL is the node's relay; the unserved workspace sees a plain server."""
    agents, owner, _ = await _setup(db_session)
    data = agents.agent.model_dump(mode="json")
    data["mcp_servers"] = [{"type": "url", "name": "data", "url": _RELAY}]
    agents.agent = BetaManagedAgentsAgent.model_validate(data)
    key = generate_signing_key()
    node = _Node(SkillsServer(skills={}))
    settings = _settings(key, workspaces=["discord:x"], node={**_NODE, "label": _LABEL})
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        detail = await client.get("/api/agents/ag_w")
    assert detail.status_code == 200, detail.text
    assert detail.json()["nodes"] == [], "the claimed node is not listed"
    assert _LABEL not in detail.text, "the node's label is not shown"


def _node(workspaces: list[str] | None) -> DataServerNode:
    settings = _settings(generate_signing_key(), workspaces=workspaces)
    assert settings.web is not None and settings.web.nodes.data_server is not None
    return DataServerNode.from_settings(settings.web.nodes.data_server, settings)


@pytest.mark.parametrize(
    ("workspaces", "workspace", "listed"),
    [
        (["discord:g-1"], "discord:g-1", True),
        (None, "discord:g-1", True),
        (["discord:x"], "discord:g-1", False),
        (["discord:g-1"], None, False),
        (None, None, True),
    ],
    ids=["listed", "unrestricted", "unlisted", "no-chat-workspace", "unrestricted-no-workspace"],
)
def test_an_unserved_node_is_never_listed_even_when_the_agent_uses_its_url(
    workspaces: list[str] | None, workspace: str | None, listed: bool
) -> None:
    """An agent whose MCP server URL claims the node shows it only where it is served."""
    claiming = agent_nodes(
        [_node(workspaces)],
        server_urls={"data": _RELAY, "other": f"{_BASE}/mcp"},
        workspace=workspace,
    )
    assert bool(claiming) is listed, f"claimed node listed={bool(claiming)} for {workspace}"
    assert all(n.label != "" for n in claiming), "a listed node keeps its label"


async def test_an_unrestricted_server_reads_no_tenant_and_a_seen_tenant_is_read_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workspace of a tenant never changes: one read serves every later check."""
    monkeypatch.setattr(node_exchange, "_WORKSPACES", {})
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-1")
    await db_session.commit()
    server = _node(["discord:g-1"]).server
    open_ = _node(None).server
    reads: list[object] = []

    class Counting:
        def __call__(self) -> AsyncSession:
            reads.append(1)
            return db_session_factory()

    assert await serves_tenant(Counting(), open_, tenant.id)  # pyright: ignore[reportArgumentType]
    assert reads == [], "an unrestricted server needs no tenant"
    assert await serves_tenant(Counting(), server, tenant.id)  # pyright: ignore[reportArgumentType]
    assert await serves_tenant(Counting(), server, tenant.id)  # pyright: ignore[reportArgumentType]
    assert len(reads) == 1, "the tenant is read once"
    assert not await serves_tenant(Counting(), _node(["discord:x"]).server, tenant.id)  # pyright: ignore[reportArgumentType]
    assert len(reads) == 1, "and then not again for another server"
