"""`/api/integrations/*` as the web app calls it: admins only, audited, and no credential shown."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web import integrations as integrations_module
from daimon.core import integration_views
from daimon.core.config import Settings
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import credential_requests, security_audit, tenants
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.agent_mcp_credentials import upsert_credential
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_account
from daimon.testing.turn_router import build_turn_router
from mux.testing.drivers.anthropic.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    shown_trail,
    signed_in,
    visitor_of,
)
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_SECRET = "test-jwt-secret-at-least-32-chars-long!!"
_URL = "https://mcp.example.com/sse"
_KEYED_URL = f"{_URL}?key=hunter2"
_SHARED = b"shared-token-value"
_AGENT = "ag_docs"


def _settings(*, mcp: bool = True) -> Settings:
    fields: dict[str, Any] = {"database": {"url": "postgresql+asyncpg://u:p@h/db"}}
    if mcp:
        fields["mcp"] = {"jwt_secret": _SECRET}
    return Settings(_env_file=None, **fields)  # pyright: ignore[reportCallIssue, reportArgumentType]


class Rig:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        web_client: WebClientFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.factory, self.tmp_path, self.web_client = factory, tmp_path, web_client
        self.agents: list[BetaManagedAgentsAgent] = [
            ma_agent(
                id=_AGENT,
                name="Docs",
                mcp_servers=[{"type": "url", "name": "docs", "url": _KEYED_URL}],
            )
        ]

        async def listed(runtime: Any, tenant_id: uuid.UUID) -> list[BetaManagedAgentsAgent]:
            return self.agents

        monkeypatch.setattr(integrations_module, "listed_agents", listed)

    @asynccontextmanager
    async def open(
        self, *, as_: str = "owner", mcp: bool = True
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Workspace]]:
        ws = await funded_workspace(self.factory)
        person = ws.person
        if as_ in ("owner", "admin"):
            async with self.factory.begin() as db:
                await roles_store.set_workspace_role(
                    db,
                    tenant_id=ws.tenant_id,
                    account_id=person.account_id,
                    role=as_,  # pyright: ignore[reportArgumentType]
                    granted_by_account_id=None,
                )
        elif as_ == "visitor":
            person = await visitor_of(self.factory, ws)
        turns = build_turns(self.factory, build_turn_router(str(ws.tenant_id)), self.tmp_path)
        token = await signed_in(self.factory, person)
        async with self.web_client(
            web_settings(), sessionmaker=self.factory, turns=turns, settings=_settings(mcp=mcp)
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            yield client, ws


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client, monkeypatch)


_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/integrations/workspaces", None),
    ("GET", "/api/integrations/github", None),
    ("GET", "/api/integrations/mcp", None),
    ("GET", "/api/integrations/mcp/expiry", None),
    ("GET", "/api/integrations/tokens", None),
    ("POST", "/api/integrations/tokens", {"scopes": ["tenant:read"]}),
    ("DELETE", f"/api/integrations/tokens/{uuid.uuid4()}", None),
]


@pytest.mark.parametrize("who", ["member", "visitor"])
async def test_members_and_visitors_are_refused_every_route(rig: Rig, who: str) -> None:
    async with rig.open(as_=who) as (client, ws):
        for method, path, body in _ROUTES:
            res = await client.request(method, path, json=body)
            assert res.status_code == 403, f"{method} {path} must be closed to a {who}: {res.text}"
        async with rig.factory() as db:
            assert await security_audit.list_recent(db, tenant_id=ws.tenant_id) == []
        assert await shown_trail(rig.factory, ws.tenant_id) == []


async def test_signed_out_callers_are_refused(rig: Rig) -> None:
    async with rig.open() as (client, _):
        client.cookies.clear()
        for method, path, body in _ROUTES:
            assert (await client.request(method, path, json=body)).status_code == 401


async def test_the_workspace_reads_as_connected(rig: Rig) -> None:
    async with rig.open(as_="admin") as (client, _):
        body = (await client.get("/api/integrations/workspaces")).json()
    assert body == {
        "workspaces": [
            {
                "platform": "discord",
                "name": "Acme",
                "status": "connected",
                "problem": False,
                "teams": [],
            }
        ]
    }


async def test_github_lists_nothing_before_it_is_connected(rig: Rig) -> None:
    async with rig.open() as (client, _):
        body = (await client.get("/api/integrations/github")).json()
    assert body == {
        "configured": False,
        "installations": [],
        "agents": [],
        "requests": [],
        "trouble": [],
    }


async def _sign_in(rig: Rig, ws: Workspace, *, agent: uuid.UUID) -> None:
    async with rig.factory.begin() as db:
        account = await make_account(db, tenant=await _tenant(db, ws))
        now = datetime.now(UTC)
        request = await credential_requests.create_credential_request(
            db,
            token=mint_request_token(),
            kind="mcp_oauth",
            tenant_id=ws.tenant_id,
            agent_id=agent,
            account_id=account.id,
            target="docs",
            mcp_server_url=_KEYED_URL,
            requester_platform_user_id="r",
            channel_id="c",
            expires_at=now + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=_AGENT,
            target_name="Docs",
            requested_work=None,
        )
        state = uuid.uuid4().hex
        await flows_store.create_flow(
            db,
            state=state,
            request_token=request.token,
            tenant_id=ws.tenant_id,
            account_id=account.id,
            agent_id=agent,
            server_name="docs",
            mcp_server_url=_KEYED_URL,
            redirect_uri="https://x.example.com/cb",
            code_verifier="v",
            expires_at=now + timedelta(hours=1),
        )
        await flows_store.mark_flow_completed(db, state=state, now=now)


async def _tenant(db: AsyncSession, ws: Workspace) -> Any:
    return await tenants.get_tenant(db, ws.tenant_id)


async def test_the_catalogue_shows_agents_and_sign_ins_but_no_credential(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        agent = derive_agent_uuid(tenant_id=ws.tenant_id, ma_agent_id=_AGENT)
        async with rig.factory.begin() as db:
            await upsert_credential(
                db,
                tenant_id=ws.tenant_id,
                agent_id=agent,
                mcp_server_url=_KEYED_URL,
                encrypted_token=_SHARED,
            )
        await _sign_in(rig, ws, agent=agent)
        res = await client.get("/api/integrations/mcp")
    assert res.status_code == 200, res.text
    (server,) = res.json()["servers"]
    assert server["name"] == "docs"
    assert server["url"] == _URL, "the query, which can carry a key, is dropped"
    assert server["signed_in"] == 1
    assert [(a["name"], a["shared"], a["signed_in"]) for a in server["agents"]] == [
        ("Docs", True, 1)
    ]
    for secret in ("hunter2", _SHARED.decode(), "key="):
        assert secret not in res.text


async def test_expiry_needs_the_provider(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async with rig.open() as (client, _):
        seen: dict[str, Any] = {}

        async def check(client_: Any, db: Any, **kwargs: Any) -> tuple[list[Any], bool]:
            seen.update(kwargs)
            return [], True

        monkeypatch.setattr(integration_views, "check_mcp_expiry", check)
        res = await client.get("/api/integrations/mcp/expiry")
    assert res.status_code == 200, res.text
    assert res.json() == {"complete": True, "servers": []}
    assert seen["server_urls"] == {_KEYED_URL}


async def test_an_owner_mints_a_token_once_and_the_list_never_holds_it(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        made = await client.post(
            "/api/integrations/tokens", json={"scopes": ["tenant:read"], "label": "ci"}
        )
        assert made.status_code == 200, made.text
        assert made.headers["cache-control"] == "no-store"
        minted = made.json()
        assert minted["scopes"] == ["tenant:read"] and minted["token"].count(".") == 2

        listing = await client.get("/api/integrations/tokens")
        trail = await shown_trail(rig.factory, ws.tenant_id)
        async with rig.factory() as db:
            events = await security_audit.list_recent(db, tenant_id=ws.tenant_id)
            row = await get_mcp_token(db, jti=uuid.UUID(minted["id"]))

    assert minted["token"] not in listing.text
    body = listing.json()
    assert body["configured"] is True and body["days"] == 30
    assert [s["id"] for s in body["scopes"]] == [
        "tenant:read",
        "channels:write",
        "agents:archive",
        "promo:redeem",
    ], "never the promo issuing scope"
    assert [(t["id"], t["label"], t["scopes"]) for t in body["tokens"]] == [
        (minted["id"], "ci", ["tenant:read"])
    ]
    assert row is not None and row.kind == "operator" and row.tenant_id == ws.tenant_id
    assert [(a, t) for a, t, _ in trail] == [("Created API token", "API token")]
    assert minted["token"] not in str(trail)
    (event,) = events
    assert (event.tool_name, event.outcome) == ("panel:operator_token_mint", "allowed")
    assert event.token_jti == uuid.UUID(minted["id"])


async def test_a_web_admin_mints_and_revokes_with_an_audit_row_each(rig: Rig) -> None:
    async with rig.open(as_="admin") as (client, ws):
        minted = (
            await client.post("/api/integrations/tokens", json={"scopes": ["channels:write"]})
        ).json()
        gone = await client.delete(f"/api/integrations/tokens/{minted['id']}")
        again = await client.delete(f"/api/integrations/tokens/{minted['id']}")
        listing = (await client.get("/api/integrations/tokens")).json()
        trail = await shown_trail(rig.factory, ws.tenant_id)
        async with rig.factory() as db:
            events = await security_audit.list_recent(db, tenant_id=ws.tenant_id)

    assert (gone.status_code, again.status_code) == (204, 404)
    assert listing["tokens"] == []
    assert [a for a, _, _ in trail] == ["Created API token", "Revoked API token"]
    assert [(e.tool_name, e.outcome, e.reason) for e in reversed(events)] == [
        ("panel:operator_token_mint", "allowed", "completed"),
        ("panel:operator_token_revoke", "allowed", "completed"),
        ("panel:operator_token_revoke", "error", "already_revoked"),
    ]


@pytest.mark.parametrize(
    "scopes", [["promo:create"], ["tenant:read", "promo:create"], ["nonsense"], []]
)
async def test_the_promo_scope_and_unknown_scopes_are_never_minted(
    rig: Rig, scopes: list[str]
) -> None:
    async with rig.open() as (client, _):
        res = await client.post("/api/integrations/tokens", json={"scopes": scopes})
        listing = (await client.get("/api/integrations/tokens")).json()
    assert res.status_code == 422
    assert "token" not in res.json()
    assert listing["tokens"] == []


async def test_without_a_signing_secret_nothing_is_minted(rig: Rig) -> None:
    async with rig.open(mcp=False) as (client, _):
        listing = (await client.get("/api/integrations/tokens")).json()
        res = await client.post("/api/integrations/tokens", json={"scopes": ["tenant:read"]})
    assert listing["configured"] is False
    assert res.status_code == 503


async def test_another_workspace_cannot_revoke_a_token(rig: Rig) -> None:
    async with rig.open() as (first, _):
        minted = (
            await first.post("/api/integrations/tokens", json={"scopes": ["tenant:read"]})
        ).json()
    async with rig.open() as (second, _):
        res = await second.delete(f"/api/integrations/tokens/{minted['id']}")
        assert res.status_code == 404
