"""Connection nodes: the data-server node's status, connect, disconnect and pass-through."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.adapters.web.app import create_app
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.adapters.web.nodes import NodeConfigError
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_changes
from daimon.core.agent_view import AgentConfigDoc, McpServer, ModelChoice
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import encode_seed, generate_signing_key
from daimon.core.stores import agent_owners
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import PUBLIC_URL, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_NOW = datetime(2030, 1, 15, 12, tzinfo=UTC)
_ISSUER = "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10"
_BASE = "https://data.example.com"
_RELAY = "https://daimon.example.com/mcp-identity/data"
_BASE_PATH = "/api/agents/ag_w/nodes/data_server"
_TOKEN = "0b0c9d3e-7c5e-4b8a-9d55-3f1f4b8a2c11"


class _Agents:
    """One agent in MA whose MCP servers move with each update."""

    def __init__(self, agent: BetaManagedAgentsAgent) -> None:
        self.agent = agent
        self.router = MARouter()
        self.router.add("GET", r"/v1/agents$", lambda _r, _m: self._list())
        self.router.add("GET", r"/v1/agents/ag_w$", lambda _r, _m: self._json())
        self.router.add("POST", r"/v1/agents/ag_w$", self._update)
        self.router.add(
            "GET",
            r"/v1/skills$",
            lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
        )

    def _json(self) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _list(self) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [self.agent.model_dump(mode="json")], "has_more": False}
        )

    def _update(self, req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        data = self.agent.model_dump(mode="json")
        data["version"] += 1
        if "mcp_servers" in body:
            data["mcp_servers"] = body["mcp_servers"]
        self.agent = BetaManagedAgentsAgent.model_validate(data)
        return self._json()


@dataclass
class _Server:
    """The identity server: token endpoint and the agent API."""

    status: int = 200
    agents: dict[str, str] = field(default_factory=dict[str, str])  # external id -> id
    manages: bool = True
    admin: bool = False
    link_status: int = 200
    calls: list[tuple[str, str, Any]] = field(default_factory=list[tuple[str, str, Any]])
    subject_tokens: list[str] = field(default_factory=list[str])
    # An OAuth error code the token endpoint answers with instead of a token.
    exchange_error: str | None = None
    exchange_status: int = 400

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/oauth/token":
            self.subject_tokens.append(parse_qs(request.content.decode())["subject_token"][0])
            if self.exchange_error is not None:
                return httpx.Response(
                    self.exchange_status,
                    json={"error": self.exchange_error, "error_description": "secret"},
                )
            return httpx.Response(200, json={"access_token": "node-at", "expires_in": 600})
        assert request.headers["Authorization"] == "Bearer node-at", "calls carry the token"
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "server says no"})
        if path.startswith("/api/agents/external/"):
            found = self.agents.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": found}) if found else httpx.Response(404)
        if (method, path) == ("POST", "/api/agents"):
            if self.link_status != 200:
                return httpx.Response(self.link_status, json={"detail": "Another person manages"})
            self.agents[body["external_id"]] = "sa-1"
            self.manages = True
            return httpx.Response(200, json={"id": "sa-1", "name": body["name"]})
        if path == "/api/agents/sa-1":
            # An admin may manage any agent; only a manager is its `managed_by`.
            agent = {
                "id": "sa-1",
                "name": "helper",
                "can_manage": self.manages or self.admin,
                "managed_by": "p-1" if self.manages else None,
            }
            return httpx.Response(200, json={**agent, "tokens": [{"id": _TOKEN}], **(body or {})})
        if path == "/api/agents/sa-1/tools":
            return httpx.Response(200, json=[{"connection_id": "c", "allow": True}])
        if path == "/api/me":
            return httpx.Response(200, json={"is_admin": self.admin})
        if path == "/api/scopes/visible":
            return httpx.Response(200, json=[{"id": "sales", "kind": "team", "name": "Sales"}])
        if (method, path) == ("DELETE", f"/api/tokens/{_TOKEN}"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


async def _setup(session: AsyncSession) -> tuple[_Agents, WebPersonRow, WebPersonRow]:
    """An agent owned by a Discord member, and another Discord member of the workspace."""
    tenant = await make_tenant(session, platform="discord", workspace_id="g-1")
    people: list[WebPersonRow] = []
    for user in ("u-1", "u-2"):
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id=user, tenant=tenant, account=account
        )
        people.append(await make_web_person(session, tenant=tenant, account=account))
    owner, other = people
    await agent_owners.record_creator(
        session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_w"),
        account_id=owner.account_id,
    )
    await session.commit()
    agent = ma_agent(
        id="ag_w",
        name="helper",
        tenant_id=tenant.id,
        metadata={"daimon_account": str(owner.account_id)},
        version=3,
        updated_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    return _Agents(agent), owner, other


async def _token(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> str:
    token = new_session_token()
    async with factory() as db, db.begin():
        await identities_store.link_identity(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="discord",
            issuer="",
            subject=str(person.id),
            label="Ada",
            groups=(),
        )
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="",
            subject=str(person.id),
            people=[person],
            ttl=_NOW - datetime(2029, 1, 1, tzinfo=UTC),
            now=_NOW,
        )
    return token


_NODE: dict[str, str] = {"identity_server": "data", "client_id": "c", "client_secret": "s"}


def _settings(
    key: Ed25519PrivateKey | None,
    *,
    workspaces: list[str] | None = None,
    node: dict[str, str] = _NODE,
) -> Settings:
    server: dict[str, object] = {
        "name": "data",
        "url": f"{_BASE}/mcp",
        "issuer": _ISSUER,
        "audience": _BASE,
    }
    if workspaces is not None:
        server["workspaces"] = workspaces
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        mcp={"public_url": "https://daimon.example.com/mcp"},  # pyright: ignore[reportArgumentType]
        mcp_identity={  # pyright: ignore[reportArgumentType]
            "signing_key": None if key is None else encode_seed(key),
            "servers": [server],
        },
        web={  # pyright: ignore[reportArgumentType]
            "public_url": PUBLIC_URL,
            "nodes": {"data_server": node},
        },
    )


@asynccontextmanager
async def _client(
    agents: _Agents,
    server: _Server,
    factory: async_sessionmaker[AsyncSession],
    person: WebPersonRow,
    *,
    key: Ed25519PrivateKey,
    workspaces: list[str] | None = None,
    settings: Settings | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    token = await _token(factory, person)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        runtime = WebRuntime(
            settings=settings or _settings(key, workspaces=workspaces),
            web=web_settings(),
            sessionmaker=factory,
            http_client=http,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            anthropic=build_fake_anthropic(agents.router.dispatch),
            identity_key=key,
            now=lambda: _NOW,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)), base_url=PUBLIC_URL
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            yield client


async def test_connect_creates_the_server_agent_and_attaches_the_relay(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    server, key = _Server(), generate_signing_key()
    async with _client(agents, server, db_session_factory, owner, key=key) as client:
        before = await client.get("/api/agents/ag_w")
        connected = await client.post(f"{_BASE_PATH}/connect", headers=_OWN)
        after = await client.get("/api/agents/ag_w")

    assert before.json()["nodes"] == [
        {"kind": "data_server", "label": "Data server", "server": None}
    ], "the node can be added"
    assert connected.status_code == 200, connected.text
    assert connected.json()["attached"] and connected.json()["reachable"], connected.json()
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    created = next(body for method, path, body in server.calls if method == "POST")
    assert created == {"name": "helper", "external_id": external}, (
        "the server agent is named for the agent and keyed by its MCP id; the token names the platform"
    )
    assert ("GET", f"/api/agents/external/{external}", None) in server.calls, "lookup by id only"
    assert connected.json()["open_url"] == f"{_BASE}/agents?agent=sa-1", connected.json()
    assert [s.url for s in agents.agent.mcp_servers] == [_RELAY], "the relay is attached"
    assert after.json()["nodes"][0]["server"] == "data", "the attached server is claimed"
    claims = jwt.decode(
        server.subject_tokens[0],
        key.public_key(),
        algorithms=["EdDSA"],
        audience=_BASE,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert (claims["sub"], claims["ws"], claims["iss"]) == (
        "discord:u-1",
        "discord:g-1",
        _ISSUER,
    ), "the exchange names the chat account and its workspace"


async def test_the_token_is_exchanged_once_and_reused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    server = _Server(agents={"x": "sa-1"})
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        await c.get(_BASE_PATH)
        await c.get(f"{_BASE_PATH}/scopes")
    assert len(server.subject_tokens) == 1, "a stored token is used until nearly spent"


async def test_disconnect_removes_the_relay_and_keeps_the_server_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    server = _Server()
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
        gone = await c.post(f"{_BASE_PATH}/disconnect", headers=_OWN)
    assert gone.status_code == 200, gone.text
    assert not gone.json()["attached"] and gone.json()["agent"]["id"] == "sa-1", gone.json()
    assert agents.agent.mcp_servers == [], "the relay is detached"
    assert not [c for c in server.calls if c[0] == "DELETE"], "the server agent stays"


async def test_a_concurrent_connect_or_disconnect_is_not_an_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents, owner, _ = await _setup(db_session)
    real = agent_changes.apply_change

    async def racing(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)  # the other request lands first
        return await real(*args, **kwargs)

    monkeypatch.setattr(agent_changes, "apply_change", racing)
    async with _client(
        agents, _Server(), db_session_factory, owner, key=generate_signing_key()
    ) as c:
        connected = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
        attached = [s.url for s in agents.agent.mcp_servers]
        gone = await c.post(f"{_BASE_PATH}/disconnect", headers=_OWN)
    assert connected.status_code == 200 and connected.json()["attached"], connected.text
    assert attached == [_RELAY], "attached once"
    assert gone.status_code == 200 and not gone.json()["attached"], gone.text


async def test_only_an_editor_of_the_agent_connects(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, _, other = await _setup(db_session)
    server = _Server()
    async with _client(agents, server, db_session_factory, other, key=generate_signing_key()) as c:
        refused = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert refused.status_code == 403, refused.text
    assert server.calls == [], "the server is never asked"


async def test_pass_through_calls_reach_the_server_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"})
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        patched = await c.patch(f"{_BASE_PATH}/agent", json={"name": "renamed"}, headers=_OWN)
        tools = await c.put(
            f"{_BASE_PATH}/tools", json={"connection_id": "c", "allow": False}, headers=_OWN
        )
        extra = await c.patch(f"{_BASE_PATH}/agent", json={"managed_by": "p-2"}, headers=_OWN)
    assert patched.json()["name"] == "renamed", patched.text
    assert ("PATCH", "/api/agents/sa-1", {"name": "renamed"}) in server.calls, "only set fields"
    assert (
        tools.status_code == 200
        and (
            "PUT",
            "/api/agents/sa-1/tools",
            {"connection_id": "c", "tool_name": None, "allow": False},
        )
        in server.calls
    ), tools.text
    assert extra.status_code == 422, "fields outside the typed change are refused here"


@pytest.mark.parametrize(
    ("status", "expected"), [(401, 403), (403, 403), (404, 404), (409, 409), (500, 502)]
)
async def test_server_refusals_map_to_clear_errors(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    status: int,
    expected: int,
) -> None:
    agents, owner, _ = await _setup(db_session)
    server = _Server(status=status)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        got = await c.get(f"{_BASE_PATH}/scopes")
    assert got.status_code == expected, got.text
    if status == 409:
        assert got.json()["detail"] == "server says no", "the server's reason is passed on"


async def test_a_refused_exchange_names_who_can_help_and_logs_the_error_code(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A server that will not exchange is not a passing problem: its admin can help."""
    agents, owner, _ = await _setup(db_session)
    server = _Server(exchange_error="unauthorized_client" + "x" * 100)
    with structlog.testing.capture_logs() as logs:
        async with _client(
            agents, server, db_session_factory, owner, key=generate_signing_key()
        ) as c:
            got = await c.get(f"{_BASE_PATH}/scopes")
    assert got.status_code == 403, got.text
    label = re.fullmatch(r"(.+) refused access\. Ask your (.+) admin\.", got.json()["detail"])
    assert label and label[1] == label[2], "the refusal names who can help, not a retry"
    refused = [e for e in logs if e["event"] == "web.node_token.refused"]
    assert [e["error"] for e in refused] == [("unauthorized_client" + "x" * 100)[:64]], (
        "the log names the OAuth error code, cut short, never its description"
    )


async def test_a_failing_token_endpoint_is_unreachable_and_not_asked_again_at_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A 5xx is the server's trouble, not a refusal; it is held off for a short while."""
    agents, owner, _ = await _setup(db_session)
    server = _Server(exchange_error="temporarily_unavailable", exchange_status=503)
    with structlog.testing.capture_logs() as logs:
        async with _client(
            agents, server, db_session_factory, owner, key=generate_signing_key()
        ) as c:
            first = await c.get(f"{_BASE_PATH}/scopes")
            again = await c.get(f"{_BASE_PATH}/scopes")
    assert (first.status_code, again.status_code) == (502, 502), first.text
    assert len(server.subject_tokens) == 1, "the server is not asked again at once"
    assert [e["event"] for e in logs if e["event"].startswith("web.node_token.")] == [
        "web.node_token.unreachable"
    ], "logged as unreachable, not refused"


async def test_a_workspace_the_server_does_not_serve_gets_no_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    server = _Server()
    async with _client(
        agents,
        server,
        db_session_factory,
        owner,
        key=generate_signing_key(),
        workspaces=["discord:x"],
    ) as c:
        detail = await c.get("/api/agents/ag_w")
        status = await c.get(_BASE_PATH)
    assert detail.json()["nodes"] == [], "the node is not offered"
    assert status.json()["reachable"] is False, status.text
    assert server.subject_tokens == [], "no exchange is attempted"


async def test_connect_takes_over_an_agent_the_platform_registered(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"}, manages=False)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        connected = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert connected.status_code == 200, connected.text
    assert any(m == "POST" for m, _p, _b in server.calls), "an unmanaged agent is linked"
    assert connected.json()["agent"]["can_manage"], connected.json()


async def test_connect_passes_on_that_another_person_manages_the_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"}, manages=False, link_status=409)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        refused = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "Another person manages", "the server's reason is shown"
    assert agents.agent.mcp_servers == [], "nothing is attached"


async def test_only_the_agents_own_tokens_are_revoked_and_none_are_made(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"})
    other = "9a1e2f3d-0000-4000-8000-000000000001"
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        revoked = await c.delete(f"{_BASE_PATH}/tokens/{_TOKEN}", headers=_OWN)
        foreign = await c.delete(f"{_BASE_PATH}/tokens/{other}", headers=_OWN)
        made = await c.post(f"{_BASE_PATH}/tokens", json={"name": "x"}, headers=_OWN)
        scopes = await c.get(f"{_BASE_PATH}/scopes")
    assert revoked.status_code == 200, revoked.text
    assert foreign.status_code == 404, "a token of another agent is not revoked here"
    assert ("DELETE", f"/api/tokens/{other}", None) not in server.calls, "never sent on"
    assert made.status_code in (404, 405), "exchanged tokens cannot make tokens: no route"
    assert scopes.json() == [{"id": "sales", "kind": "team", "name": "Sales"}], scopes.text


async def test_connect_links_an_unmanaged_agent_for_a_server_admin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An admin passes `can_manage` on an agent nobody manages, which still needs linking."""
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"}, manages=False, admin=True)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        connected = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert connected.status_code == 200, connected.text
    assert any(m == "POST" for m, _p, _b in server.calls), "linked despite can_manage"
    assert connected.json()["agent"]["managed_by"] == "p-1", "the agent has a manager now"


async def test_connect_skips_linking_an_agent_the_caller_manages(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"}, manages=True)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        connected = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert connected.status_code == 200, connected.text
    assert not any(m == "POST" for m, _p, _b in server.calls), "nothing to link"


async def test_connect_tolerates_a_manager_conflict_for_an_admin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The race: another person took the agent between the lookup and the link."""
    agents, owner, _ = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"}, manages=False, admin=True, link_status=409)
    async with _client(agents, server, db_session_factory, owner, key=generate_signing_key()) as c:
        connected = await c.post(f"{_BASE_PATH}/connect", headers=_OWN)
    assert connected.status_code == 200, connected.text
    assert [s.url for s in agents.agent.mcp_servers] == [_RELAY], "the relay is attached"


async def test_proxied_writes_need_edit_rights_on_the_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await _setup(db_session)
    external = str(derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"))
    server = _Server(agents={external: "sa-1"})
    async with _client(agents, server, db_session_factory, other, key=generate_signing_key()) as c:
        patched = await c.patch(f"{_BASE_PATH}/agent", json={"name": "x"}, headers=_OWN)
        tool = await c.put(
            f"{_BASE_PATH}/tools", json={"connection_id": "c", "allow": False}, headers=_OWN
        )
        revoked = await c.delete(f"{_BASE_PATH}/tokens/{_TOKEN}", headers=_OWN)
    assert (patched.status_code, tool.status_code, revoked.status_code) == (403, 403, 403), (
        "a viewer of the agent changes nothing on the server"
    )
    assert server.calls == [], "the server is never asked"


async def test_status_of_an_agent_whose_connections_the_viewer_cannot_see_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A member's view hides attached servers, so `attached` must not tell."""
    agents, owner, other = await _setup(db_session)
    server = _Server()
    async with _client(agents, server, db_session_factory, other, key=generate_signing_key()) as c:
        status = await c.get(_BASE_PATH)
        detail = await c.get("/api/agents/ag_w")
    assert status.status_code == 403, status.text
    assert server.calls == [], "the server is never asked"
    assert detail.json()["nodes"] == [], "a node is not offered where its state is hidden"


def test_attaching_the_relay_twice_changes_nothing() -> None:
    """Two connects at once both reach the change; the second finds the relay there."""
    settings = _settings(generate_signing_key())
    node = DataServerNode.from_settings(
        settings.web.nodes.data_server,  # pyright: ignore[reportOptionalMemberAccess,reportArgumentType]
        settings,
    )
    empty = AgentConfigDoc(
        name="helper",
        purpose="",
        avatar="",
        model=ModelChoice(
            id="claude-sonnet-4-6", backend="anthropic", speed="standard", effort="high"
        ),
        system=None,
        skills=[],
        tools=[],
        mcp_servers=[],
        places=None,
    )
    once = node._with_relay(empty, _RELAY)  # pyright: ignore[reportPrivateUsage]
    twice = node._with_relay(once, _RELAY)  # pyright: ignore[reportPrivateUsage]
    assert len(once.mcp_servers) == 1, "the first attaches"
    assert twice == once, "the second leaves the config as it is"
    assert isinstance(once.mcp_servers[0], McpServer), "an MCP server entry"


@pytest.mark.parametrize(
    ("node", "key", "match"),
    [
        ({"identity_server": "data", "client_id": "c"}, True, "CLIENT_SECRET"),
        ({"client_secret": "s"}, True, "IDENTITY_SERVER, .*CLIENT_ID"),
        ({**_NODE, "identity_server": "missing"}, True, "IDENTITY_SERVER names 'missing'"),
        (_NODE, False, "SIGNING_KEY"),
    ],
    ids=["no-secret", "secret-only", "unknown-server", "no-signing-key"],
)
async def test_the_web_app_refuses_to_start_with_an_incomplete_node(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    node: dict[str, str],
    key: bool,
    match: str,
) -> None:
    agents, owner, _ = await _setup(db_session)
    signing = generate_signing_key() if key else None
    with pytest.raises(NodeConfigError, match=match):
        async with _client(
            agents,
            _Server(),
            db_session_factory,
            owner,
            key=generate_signing_key(),
            settings=_settings(signing, node=node),
        ):
            pass
