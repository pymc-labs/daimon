"""The identity relay signs who is calling and forwards to the opted-in server only."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
import jwt as pyjwt
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.identity_relay import (
    RELAY_METHODS,
    RELAY_ROUTE,
    build_identity_relay_route,
)
from daimon.core.config import SignedIdentityServer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_identity import IdentityTokenCache, SigningKey, generate_signing_key
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

SECRET = b"r" * 32
SERVER = SignedIdentityServer(
    name="tools",
    url="https://tools.example.com/mcp",
    issuer="daimon-test",
    audience="https://tools.example.com",
)


class _Upstream:
    def __init__(self, *, down: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.down = down

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        self.requests.append(request)
        return httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
            headers={"content-type": "application/json", "mcp-session-id": "up-1"},
        )


async def _caller(
    factory: async_sessionmaker[AsyncSession], *, with_turn: bool
) -> tuple[str, uuid.UUID]:
    """A Discord person's chat token for agent_a, optionally inside a running turn."""
    now = dt.datetime.now(dt.UTC)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord", workspace_id="guild-1")
        principal = await make_platform_principal(
            db, platform="discord", external_id="user-1", tenant=tenant
        )
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_a")
        token, _, _ = await mint_chat_mcp_token(
            db, account_id=principal.account_id, agent_id=agent_id, secret=SECRET, now=now
        )
        if with_turn:
            await create_origin(
                db, tenant_id=tenant.id, account_id=principal.account_id, platform="discord",
                parent_channel_id="chan-1", thread_id="thread-1",
                responder_ma_agent_id="agent_a", responder_name="Helper",
                configuration_target_ma_agent_id=None, configuration_target_name=None,
                role=Role.USER, expires_at=now + dt.timedelta(hours=1), now=now,
                sender_foreign=False, shared_channel=False,
                platform_channel_id="chan-1", platform_message_id="msg-1",
            )  # fmt: skip
    return token, agent_id


def _relay(
    factory: async_sessionmaker[AsyncSession], upstream: _Upstream, key
) -> httpx.AsyncClient:
    app = Starlette()
    app.add_route(
        RELAY_ROUTE,
        build_identity_relay_route(
            servers={SERVER.name: SERVER},
            auth=DaimonJWTVerifier(secret=SECRET, sessionmaker=factory),
            sessionmaker=factory,
            tokens=IdentityTokenCache(key),
            http=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
            clock=lambda: dt.datetime.now(dt.UTC),
        ),
        methods=RELAY_METHODS,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.test")


def _identity(request: httpx.Request, key: SigningKey) -> dict[str, object]:
    token = request.headers["authorization"].removeprefix("Bearer ")
    return pyjwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience=SERVER.audience)


async def test_person_turn_is_signed_and_forwarded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, agent_id = await _caller(db_session_factory, with_turn=True)
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        response = await client.post(
            "/mcp-identity/tools",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "mcp-session-id": "up-1",
            },
        )
    assert response.status_code == 200, "the server's answer passes back"
    assert response.headers["mcp-session-id"] == "up-1", "the server's session id passes back"
    (sent,) = upstream.requests
    assert str(sent.url) == SERVER.url, "the request goes to the configured server"
    assert sent.headers["mcp-session-id"] == "up-1", "the client's session id is forwarded"
    claims = _identity(sent, key)
    assert claims["sub"] == "discord:user-1", "the person who asked is named"
    assert claims["ws"] == "discord:guild-1", "the guild is named"
    assert (claims["channel"], claims["msg"]) == ("chan-1", "msg-1"), "the turn's message"
    assert claims["agent"] == str(agent_id) and claims["agent_name"] == "Helper", "the agent"
    assert claims["iss"] == "daimon-test", "the configured issuer"
    assert token not in json.dumps(dict(sent.headers)), "Daimon's own credential stays here"


async def test_call_outside_a_person_turn_names_no_person(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(db_session_factory, with_turn=False)
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        await client.post("/mcp-identity/tools", headers={"authorization": f"Bearer {token}"})
    claims = _identity(upstream.requests[0], key)
    assert not {"sub", "msg", "channel"} & set(claims), "a routine's call names no person"
    assert claims["ws"] == "discord:guild-1", "the workspace is still named"


async def test_unknown_server_and_bad_credentials_never_reach_a_server(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(db_session_factory, with_turn=True)
    upstream = _Upstream()
    async with _relay(db_session_factory, upstream, generate_signing_key()) as client:
        unknown = await client.post(
            "/mcp-identity/other", headers={"authorization": f"Bearer {token}"}
        )
        missing = await client.post("/mcp-identity/tools")
        forged = await client.post(
            "/mcp-identity/tools", headers={"authorization": "Bearer not-a-token"}
        )
    assert unknown.status_code == 404, "only configured servers are relayed"
    assert (missing.status_code, forged.status_code) == (401, 401), "a credential is required"
    assert upstream.requests == [], "nothing was forwarded"


async def test_unreachable_server_is_a_bad_gateway(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(db_session_factory, with_turn=True)
    async with _relay(db_session_factory, _Upstream(down=True), generate_signing_key()) as client:
        response = await client.post(
            "/mcp-identity/tools", headers={"authorization": f"Bearer {token}"}
        )
    assert response.status_code == 502, "a server that cannot be reached is a 502"
