"""The identity relay signs who is calling and forwards to the opted-in server only."""

from __future__ import annotations

import datetime as dt
import json
import ssl
import uuid
from dataclasses import dataclass

import httpx
import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.identity_relay import (
    MAX_BODY_BYTES,
    RELAY_METHODS,
    RELAY_ROUTE,
    build_identity_relay_route,
    build_relay_clients,
)
from daimon.core.config import SignedIdentityServer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_identity import (
    IdentityTokenCache,
    RelayBinding,
    SigningKey,
    generate_signing_key,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import create_thread_session, mark_dead
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


def _public_ca() -> tuple[str, bytes]:
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic relay CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(dt.datetime(2025, 1, 1, tzinfo=dt.UTC))
        .not_valid_after(dt.datetime(2035, 1, 1, tzinfo=dt.UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, algorithm=None)
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
        certificate.public_bytes(serialization.Encoding.DER),
    )


class _Upstream:
    def __init__(self, *, down: bool = False, busy: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.down = down
        self.busy = busy

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if self.busy:
            raise httpx.PoolTimeout("no free connection", request=request)
        self.requests.append(request)
        return httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
            headers={"content-type": "application/json", "mcp-session-id": "up-1"},
        )


@dataclass
class _Person:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID


async def _person(factory: async_sessionmaker[AsyncSession]) -> _Person:
    """A Discord person in guild-1 who talks to agent_a."""
    async with factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord", workspace_id="guild-1")
        principal = await make_platform_principal(
            db, platform="discord", external_id="user-1", tenant=tenant
        )
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_a")
    return _Person(tenant.id, principal.account_id, agent_id)


async def _token(
    factory: async_sessionmaker[AsyncSession], person: _Person, relay: RelayBinding | None
) -> str:
    async with factory() as db, db.begin():
        token, _, _ = await mint_chat_mcp_token(
            db, account_id=person.account_id, agent_id=person.agent_id, secret=SECRET,
            now=dt.datetime.now(dt.UTC), relay=relay,
        )  # fmt: skip
    return token


async def _chat_session(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    thread: str,
    *,
    turn: bool = True,
    foreign: bool = False,
) -> tuple[str, uuid.UUID]:
    """A person's chat session in `thread`, optionally running their turn there.

    Returns the session's relay credential and its mapping row id.
    """
    now = dt.datetime.now(dt.UTC)
    mapping_id = uuid.uuid4()
    async with factory() as db, db.begin():
        await create_thread_session(
            db, id=mapping_id, tenant_id=person.tenant_id, platform="discord",
            thread_id=thread, account_id=person.account_id, ma_session_id=f"sess_{thread}",
            ma_agent_id="agent_a",
        )  # fmt: skip
        if turn:
            await create_origin(
                db, tenant_id=person.tenant_id, account_id=person.account_id,
                platform="discord", parent_channel_id="chan-1", thread_id=thread,
                responder_ma_agent_id="agent_a", responder_name="Helper",
                configuration_target_ma_agent_id=None, configuration_target_name=None,
                role=Role.USER, expires_at=now + dt.timedelta(hours=1), now=now,
                sender_foreign=foreign, shared_channel=False,
                platform_channel_id=thread, platform_message_id=f"msg-{thread}",
            )  # fmt: skip
    token = await _token(factory, person, RelayBinding("Agent A", mapping_id))
    return token, mapping_id


def _relay(
    factory: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    key: SigningKey,
    server: SignedIdentityServer = SERVER,
) -> httpx.AsyncClient:
    app = Starlette()
    app.add_route(
        RELAY_ROUTE,
        build_identity_relay_route(
            servers={server.name: server},
            auth=DaimonJWTVerifier(secret=SECRET, sessionmaker=factory),
            sessionmaker=factory,
            tokens=IdentityTokenCache(key),
            clients={
                server.name: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle))
            },
            clock=lambda: dt.datetime.now(dt.UTC),
        ),
        methods=RELAY_METHODS,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.test")


def _identity(request: httpx.Request, key: SigningKey) -> dict[str, object]:
    token = request.headers["authorization"].removeprefix("Bearer ")
    return pyjwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience=SERVER.audience)


def test_relay_ca_is_scoped_to_one_https_client(monkeypatch: pytest.MonkeyPatch) -> None:
    pem, der = _public_ca()
    private = SignedIdentityServer(
        name="private",
        url=SERVER.url,
        issuer=SERVER.issuer,
        audience=SERVER.audience,
        ca_certificate=pem,
    )
    ordinary = SERVER.model_copy(update={"name": "ordinary"})
    captured: list[object] = []

    class CaptureClient:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs.get("verify"))

    monkeypatch.setattr("daimon.adapters.mcp.identity_relay.httpx.AsyncClient", CaptureClient)
    build_relay_clients([private, ordinary])
    custom, normal = captured
    assert isinstance(custom, ssl.SSLContext)
    assert custom.verify_mode == ssl.CERT_REQUIRED and custom.check_hostname
    assert der in custom.get_ca_certs(binary_form=True)
    assert normal is True

    with pytest.raises(ValueError, match="valid PEM certificate"):
        SignedIdentityServer(
            name="invalid",
            url=SERVER.url,
            issuer=SERVER.issuer,
            audience=SERVER.audience,
            ca_certificate="not a PEM certificate",
        )


async def _call(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/mcp-identity/tools", headers={"authorization": f"Bearer {token}"})


async def test_person_turn_is_signed_and_forwarded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
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
    assert (claims["channel"], claims["msg"]) == ("thread-1", "msg-thread-1"), "the turn's message"
    assert claims["agent"] == str(person.agent_id), "the agent"
    assert claims["agent_name"] == "Helper", "the name the turn answered under"
    assert claims["iss"] == "daimon-test", "the configured issuer"
    assert token not in json.dumps(dict(sent.headers)), "Daimon's own credential stays here"


async def test_two_live_turns_of_one_person_each_get_their_own_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    first, _ = await _chat_session(db_session_factory, person, "thread-1")
    second, _ = await _chat_session(db_session_factory, person, "thread-2")
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        await _call(client, second)
        await _call(client, first)
    named = [_identity(request, key)["msg"] for request in upstream.requests]
    assert named == ["msg-thread-2", "msg-thread-1"], "each session names its own turn"


async def test_routine_beside_a_live_turn_names_no_person(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _chat_session(db_session_factory, person, "thread-1")
    routine = await _token(db_session_factory, person, RelayBinding("Agent A"))
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        await _call(client, routine)
    claims = _identity(upstream.requests[0], key)
    assert not {"sub", "msg", "channel"} & set(claims), "a routine is never the chat turn"
    assert claims["ws"] == "discord:guild-1", "the workspace is still named"
    assert claims["agent_name"] == "Agent A", "the agent is named outside a turn too"


async def test_stale_idle_and_foreign_sessions_name_no_person(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    stale, stale_id = await _chat_session(db_session_factory, person, "thread-1")
    async with db_session_factory() as db, db.begin():
        await mark_dead(db, id=stale_id)
    await _chat_session(db_session_factory, person, "thread-1")  # its successor's turn
    idle, _ = await _chat_session(db_session_factory, person, "thread-2", turn=False)
    foreign, _ = await _chat_session(db_session_factory, person, "thread-3", foreign=True)
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        for token in (stale, idle, foreign):
            await _call(client, token)
    for case, request in zip(("replaced", "idle", "foreign"), upstream.requests, strict=True):
        assert "sub" not in _identity(request, key), f"a {case} session names no person"


async def test_only_a_relay_credential_from_an_allowed_workspace_is_relayed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    own = await _token(db_session_factory, person, None)
    other_workspace = SERVER.model_copy(update={"workspaces": frozenset({"discord:guild-2"})})
    upstream = _Upstream()
    async with _relay(db_session_factory, upstream, generate_signing_key()) as client:
        chat = await _call(client, own)
        too_big = await client.post(
            "/mcp-identity/tools",
            content=b"x" * (MAX_BODY_BYTES + 1),
            headers={"authorization": f"Bearer {token}"},
        )
    async with _relay(
        db_session_factory, upstream, generate_signing_key(), other_workspace
    ) as client:
        refused = await _call(client, token)
    assert chat.status_code == 403, "Daimon's own chat credential is not a relay credential"
    assert too_big.status_code == 413, "an oversized body is refused"
    assert refused.status_code == 403, "a server limited to other workspaces is refused"
    assert upstream.requests == [], "nothing was forwarded"


async def test_unknown_server_and_bad_credentials_never_reach_a_server(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
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


async def test_unreachable_server_is_a_bad_gateway_and_a_full_pool_a_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    async with _relay(db_session_factory, _Upstream(down=True), generate_signing_key()) as client:
        down = await _call(client, token)
    async with _relay(db_session_factory, _Upstream(busy=True), generate_signing_key()) as client:
        busy = await _call(client, token)
    assert down.status_code == 502, "a server that cannot be reached is a 502"
    assert busy.status_code == 503, "every connection to the server is in use"
    assert busy.headers["retry-after"] == "5", "the caller is told when to retry"
