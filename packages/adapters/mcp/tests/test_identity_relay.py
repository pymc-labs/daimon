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
from anthropic import AsyncAnthropic
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from daimon.adapters.mcp import identity_relay
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.identity_relay import (
    MAX_BODY_BYTES,
    RELAY_METHODS,
    RELAY_ROUTE,
    build_identity_relay_route,
    build_relay_clients,
)
from daimon.adapters.mcp.tools import _session_gate
from daimon.core.agent_chat_requester import record_driver
from daimon.core.config import SignedIdentityServer
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_identity import (
    IdentityTokenCache,
    RelayBinding,
    SigningKey,
    generate_signing_key,
)
from daimon.core.ops_events import OpsEvent
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.stores.routines import create_routine
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import create_thread_session, mark_dead
from daimon.core.stores.turn_origins import bind_origin_session, create_origin
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from pydantic import SecretStr
from sqlalchemy import text
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
    def __init__(self, *, down: bool = False, busy: bool = False, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.down = down
        self.busy = busy
        self.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if self.busy:
            raise httpx.PoolTimeout("no free connection", request=request)
        self.requests.append(request)
        return httpx.Response(
            self.status,
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


async def _colleague(factory: async_sessionmaker[AsyncSession], person: _Person) -> _Person:
    """user-2, in the same guild and talking to the same agent as `person`."""
    async with factory() as db, db.begin():
        tenant = await get_tenant(db, person.tenant_id)
        principal = await make_platform_principal(
            db, platform="discord", external_id="user-2", tenant=tenant
        )
    return _Person(person.tenant_id, principal.account_id, person.agent_id)


async def _token(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    relay: RelayBinding | None,
    hold: TurnHold | None = None,
) -> str:
    async with factory() as db, db.begin():
        token, _, _ = await mint_chat_mcp_token(
            db, account_id=person.account_id, agent_id=person.agent_id, secret=SECRET,
            now=dt.datetime.now(dt.UTC), relay=relay, hold=hold,
        )  # fmt: skip
    return token


async def _chat_session(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    thread: str,
    *,
    turn: bool = True,
    foreign: bool = False,
    human: bool | None = True,
    dm: bool = False,
    channel: str | None = None,
    grant: str | None = None,
    message: str | None = None,
) -> tuple[str, uuid.UUID]:
    """A person's chat session in `thread`, optionally running their turn there.

    With a `grant` the session has an execution grant, which its turn holds.
    Returns the session's relay credential and its mapping row id.
    """
    now = dt.datetime.now(dt.UTC)
    mapping_id = uuid.uuid4()
    async with factory() as db, db.begin():
        await create_thread_session(
            db, id=mapping_id, tenant_id=person.tenant_id, platform="discord",
            thread_id=thread, account_id=person.account_id,
            ma_session_id=f"sess_{grant or thread}",
            ma_agent_id="agent_a", channel_id=channel,
        )  # fmt: skip
        if turn:
            origin = await create_origin(
                db, tenant_id=person.tenant_id, account_id=person.account_id,
                platform="discord", parent_channel_id="chan-1", thread_id=thread,
                responder_ma_agent_id="agent_a", responder_name="Helper",
                configuration_target_ma_agent_id=None, configuration_target_name=None,
                role=Role.USER, expires_at=now + dt.timedelta(hours=1), now=now,
                sender_foreign=foreign, shared_channel=False,
                platform_channel_id=None if dm else thread,
                platform_message_id=message or f"msg-{thread}",
            )  # fmt: skip
            await bind_origin_session(
                db, origin_id=origin.id, chat_session_id=grant, human_invocation=human
            )
    token = await _token(factory, person, RelayBinding("Agent A", mapping_id, grant=grant))
    return token, mapping_id


def _relay(
    factory: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    key: SigningKey | None,
    server: SignedIdentityServer = SERVER,
    bearers: dict[str, SecretStr] | None = None,
    anthropic: AsyncAnthropic | None = None,
) -> httpx.AsyncClient:
    app = Starlette()
    app.add_route(
        RELAY_ROUTE,
        build_identity_relay_route(
            servers={server.name: server},
            auth=DaimonJWTVerifier(secret=SECRET, sessionmaker=factory),
            sessionmaker=factory,
            tokens=IdentityTokenCache(key) if key is not None else None,
            bearers=bearers,
            anthropic=anthropic,
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


async def test_each_relay_failure_is_an_issue_with_its_own_reason(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Busy, unconfigured and an upstream 503 differ; a valid credential to an unknown
    server is still its tenant's issue."""
    reported: list[OpsEvent] = []
    monkeypatch.setattr(
        identity_relay, "report_ops_event", lambda sm, event, *, logged: reported.append(event)
    )
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    key = generate_signing_key()
    async with _relay(db_session_factory, _Upstream(busy=True), key) as client:
        await _call(client, token)
        await client.post("/mcp-identity/other", headers={"authorization": f"Bearer {token}"})
    async with _relay(db_session_factory, _Upstream(), None, BEARER_SERVER) as client:
        await _post(client, token, _tool_call())
    async with _relay(db_session_factory, _Upstream(status=503), key) as client:
        await _call(client, token)
    assert [(e.code, e.tenant_id, e.reason, e.subject_key) for e in reported] == [
        ("bridge.relay_failed", person.tenant_id, "relay_busy", "tools"),
        ("bridge.relay_refused", person.tenant_id, "404", None),
        ("bridge.relay_failed", person.tenant_id, "relay_unconfigured", "tools"),
        ("bridge.relay_failed", person.tenant_id, "503", "tools"),
    ]


BEARER_KEY = "bearer-key-1"
BEARER_SERVER = SignedIdentityServer(
    name="tools",
    url="https://tools.example.com/mcp",
    auth="bearer",
    workspaces=frozenset({"discord:guild-1"}),
    agents=("Agent A", "Helper"),
    requester_meta=True,
)
_BEARERS = {"tools": SecretStr(BEARER_KEY)}


def _tool_call(**meta: object) -> str:
    params: dict[str, object] = {"name": "send", "arguments": {"text": "hi"}}
    if meta:
        params["_meta"] = meta
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


async def _post(client: httpx.AsyncClient, token: str, body: str | bytes) -> httpx.Response:
    return await client.post(
        "/mcp-identity/tools",
        content=body,
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
    )


def _sent_meta(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)["params"]["_meta"]


async def test_bearer_person_turn_is_stamped_and_the_key_replaces_the_credential(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        response = await _post(client, token, _tool_call())
    assert response.status_code == 200, "the server's answer passes back"
    (sent,) = upstream.requests
    assert sent.headers["authorization"] == f"Bearer {BEARER_KEY}", "the configured key is sent"
    assert token not in json.dumps(dict(sent.headers)), "the inbound credential stays here"
    assert _sent_meta(sent) == {
        "requester_platform_user_id": "user-1",
        "requester_conversation": {
            "place_type": "thread",
            "channel_id": "thread-1",
            "parent_channel_id": "chan-1",
        },
        "requester_kind": "person",
    }, "the live turn's asker and place are stamped"


WAKE_SERVER = BEARER_SERVER.model_copy(update={"wake_tools": frozenset({"send"})})


@pytest.fixture
def callbacks_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.setenv("DAIMON_WATCHES__CALLBACK_BASE_URL", "https://hooks.example.com")


async def _wake_watches(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, object]]:
    async with factory() as db:
        rows = await db.execute(
            text("SELECT thread_id, expires_at FROM watches WHERE source = 'callback'")
        )
        return [(str(thread), expires_at) for thread, expires_at in rows]


async def _wake_urls(
    factory: async_sessionmaker[AsyncSession], token: str, *bodies: str
) -> list[object]:
    upstream = _Upstream()
    async with _relay(factory, upstream, None, WAKE_SERVER, bearers=_BEARERS) as client:
        for body in bodies:
            response = await _post(client, token, body)
            assert response.status_code == 200, "the call runs, with or without a link"
    return [_sent_meta(sent).get("wake_url") for sent in upstream.requests]


async def test_wake_tool_calls_get_their_own_links_to_one_watch_per_conversation(
    db_session_factory: async_sessionmaker[AsyncSession], callbacks_on: None
) -> None:
    """Each job gets a link that still works when the next one starts; they share one watch,
    which lasts a day after the latest call."""
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1", channel="chan-1")
    other = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read"}}
    )
    before = dt.datetime.now(dt.UTC)
    first, second, third = await _wake_urls(
        db_session_factory,
        token,
        _tool_call(wake_url="https://forged.example"),
        _tool_call(),
        other,
    )
    assert str(first).startswith("https://hooks.example.com/callbacks/watches/"), (
        "a person's wake tool call carries Daimon's link, not the caller's"
    )
    assert isinstance(second, str) and second != first, "the next job gets its own link"
    assert second.rsplit("/", 2)[1] == str(first).rsplit("/", 2)[1], "to the same watch"
    assert third is None, "another tool gets no link"
    ((thread, expires_at),) = await _wake_watches(db_session_factory)
    assert thread == "thread-1", "one callback watch, bound to the asking thread"
    assert isinstance(expires_at, dt.datetime)
    assert abs(expires_at - (before + dt.timedelta(days=1))) < dt.timedelta(minutes=1), (
        "the watch lasts a day"
    )


async def test_a_wake_watch_follows_the_conversation_to_its_newest_session(
    db_session_factory: async_sessionmaker[AsyncSession], callbacks_on: None
) -> None:
    """A relay-scoped thread gets a fresh session every turn: the link made in one turn
    must still reach the thread after the person talks again."""
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1", channel="chan-1")
    await _wake_urls(db_session_factory, token, _tool_call())
    async with db_session_factory() as db, db.begin():
        await create_thread_session(
            db, tenant_id=person.tenant_id, platform="discord", thread_id="thread-1",
            account_id=person.account_id, ma_session_id="sess_next", ma_agent_id="agent_a",
            channel_id="chan-1", created_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
        )  # fmt: skip
    async with db_session_factory() as db:
        bound = (await db.execute(text("SELECT session_id FROM watches"))).scalars().all()
    assert bound == ["sess_next"], "the wake watch moved to the thread's newest session"


async def test_a_wake_link_made_in_a_dm_wakes_that_dm(
    db_session_factory: async_sessionmaker[AsyncSession], callbacks_on: None
) -> None:
    """A DM's jobs get links too: each post runs a fresh private turn in the owner's DM,
    so the watch holds no session, and a later job's link shares its one watch."""
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "dm:scope-1", channel="chan-1")
    first, second = await _wake_urls(db_session_factory, token, _tool_call(), _tool_call())
    assert isinstance(first, str) and isinstance(second, str), "a DM's job gets a wake link"
    assert first.rsplit("/", 2)[1] == second.rsplit("/", 2)[1], "both jobs share one watch"
    async with db_session_factory() as db:
        rows = (await db.execute(text("SELECT delivery, session_id FROM watches"))).all()
    assert [tuple(row) for row in rows] == [("dm", None)], "it reports in the owner's DM"


@pytest.mark.parametrize("case", ["unattended", "owner limit", "helper"])
async def test_a_wake_tool_call_that_cannot_have_a_link_still_runs_without_one(
    db_session_factory: async_sessionmaker[AsyncSession], callbacks_on: None, case: str
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(
        db_session_factory, person, "thread-1", channel="chan-1", human=case != "unattended"
    )
    if case == "owner limit":
        await _fill_owner_watches(db_session_factory, person)
    if case == "helper":
        async with db_session_factory() as db, db.begin():
            await db.execute(text("UPDATE turn_origins SET helper_of = id"))
    (wake_url,) = await _wake_urls(db_session_factory, token, _tool_call())
    assert wake_url is None, f"{case}: no link"
    made = len(await _wake_watches(db_session_factory))
    assert made == (25 if case == "owner limit" else 0), f"{case}: no wake watch is made"


async def _fill_owner_watches(factory: async_sessionmaker[AsyncSession], person: _Person) -> None:
    """The owner already holds the most live watches one person may."""
    binding = ContinuationRequest(
        tenant_id=person.tenant_id, platform="discord", parent_channel_id="chan-1",
        thread_id="thread-1", requester_account_id=person.account_id,
        requester_external_user_id="user-1", target_ma_agent_id="agent_a",
        target_name="Helper", requested_work=None, reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )  # fmt: skip
    async with factory() as db, db.begin():
        for _ in range(25):
            await watches.register_callback_watch(
                db, binding, instructions="Report.", lifetime=dt.timedelta(days=1),
                now=dt.datetime.now(dt.UTC),
            )  # fmt: skip


async def test_a_wake_link_is_made_without_the_approval_card(
    db_session_factory: async_sessionmaker[AsyncSession],
    callbacks_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate: `create_watch` needs the owner's card, a wake link does not. The person's
    own call started the job, the link goes only to the server they called and never to a
    channel, and it can only wake their conversation to report."""

    async def no_card(*args: object, **kwargs: object) -> None:
        raise AssertionError("a wake link never asks for the approval card")

    monkeypatch.setattr(_session_gate, "session_card_gap", no_card)
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1", channel="chan-1")
    (wake_url,) = await _wake_urls(db_session_factory, token, _tool_call())
    assert wake_url is not None, "the link is made with no card"


async def test_forged_requester_fields_are_replaced_on_a_person_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    forged = _tool_call(
        requester_platform_user_id="666",
        requester_conversation={"place_type": "dm", "channel_id": "evil"},
        requester_kind="routine",
    )
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, forged)
    meta = _sent_meta(upstream.requests[0])
    assert meta["requester_platform_user_id"] == "user-1", "a forged asker is overwritten"
    assert meta["requester_conversation"] != {"place_type": "dm", "channel_id": "evil"}, (
        "a forged place is overwritten"
    )
    assert meta["requester_kind"] == "person", "a forged kind is overwritten"


async def test_forged_requester_fields_are_stripped_when_no_person_is_live(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    routine = await _token(db_session_factory, person, RelayBinding("Agent A"))
    idle, _ = await _chat_session(db_session_factory, person, "thread-2", turn=False)
    foreign, _ = await _chat_session(db_session_factory, person, "thread-3", foreign=True)
    upstream = _Upstream()
    forged = _tool_call(requester_platform_user_id="666", requester_conversation={"x": "y"})
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        for token in (routine, idle, foreign):
            await _post(client, token, forged)
    assert len(upstream.requests) == 3, "the calls still go through, as the key owner's"
    for case, request in zip(("routine", "idle", "foreign"), upstream.requests, strict=True):
        assert _sent_meta(request) == {}, f"a {case} call carries no requester, forged or not"


async def _routine(
    factory: async_sessionmaker[AsyncSession], person: _Person, **fields: object
) -> uuid.UUID:
    async with factory() as db, db.begin():
        row = await create_routine(
            db,
            tenant_id=person.tenant_id,
            created_by_user_id=fields.pop("owner", "user-1"),  # type: ignore[arg-type]
            agent_id=fields.pop("agent_id", "agent_a"),  # type: ignore[arg-type]
            agent_name="Agent A",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="go",
            **fields,  # type: ignore[arg-type]
        )
    return row.id


@pytest.mark.parametrize(
    ("fields", "conversation"),
    [
        (
            {"destination_kind": "channel", "destination_id": "chan-9"},
            {"place_type": "channel", "channel_id": "chan-9", "parent_channel_id": None},
        ),
        (
            {"destination_kind": "thread", "destination_id": "thr-9", "channel_id": "chan-9"},
            {"place_type": "thread", "channel_id": "thr-9", "parent_channel_id": "chan-9"},
        ),
        (
            {"destination_kind": "dm", "owner": "1234"},
            {"place_type": "dm", "channel_id": None, "parent_channel_id": None},
        ),
        ({}, None),
    ],
)
async def test_routine_run_is_stamped_with_its_owner_delivery_place_and_kind(
    db_session_factory: async_sessionmaker[AsyncSession],
    fields: dict[str, object],
    conversation: dict[str, object] | None,
) -> None:
    person = await _person(db_session_factory)
    routine_id = await _routine(db_session_factory, person, **fields)
    owner = str(fields.get("owner", "user-1"))
    token = await _token(
        db_session_factory, person, RelayBinding("Agent A", routine_id=routine_id, owner=owner)
    )
    upstream = _Upstream()
    forged = _tool_call(
        requester_platform_user_id="666",
        requester_conversation={"place_type": "dm", "channel_id": "evil"},
        requester_kind="person",
    )
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, forged)
    expected: dict[str, object] = {
        "requester_platform_user_id": owner,
        "requester_kind": "routine",
    }
    if conversation is not None:
        expected["requester_conversation"] = conversation
    assert _sent_meta(upstream.requests[0]) == expected, "the owner, not a forged asker"


async def test_a_routine_credential_that_names_no_live_routine_stamps_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    async with db_session_factory() as db, db.begin():
        other = _Person(
            (await make_tenant(db, platform="discord", workspace_id="guild-2")).id,
            person.account_id,
            person.agent_id,
        )
    cases = {
        "deleted": uuid.uuid4(),
        "another tenant's": await _routine(
            db_session_factory, other, destination_kind="channel", destination_id="c"
        ),
        "another agent's": await _routine(db_session_factory, person, agent_id="agent_b"),
        "ownerless": await _routine(db_session_factory, person, owner=None),
        "handed to someone else": await _routine(db_session_factory, person, owner="user-2"),
    }
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        for routine_id in cases.values():
            token = await _token(
                db_session_factory,
                person,
                RelayBinding("Agent A", routine_id=routine_id, owner="user-1"),
            )
            await _post(
                client,
                token,
                _tool_call(requester_platform_user_id="666", requester_kind="routine"),
            )
    assert [_sent_meta(r) for r in upstream.requests] == [{}] * len(cases), (
        "a routine that is gone, foreign, another agent's or ownerless names nobody"
    )


async def test_an_ownership_change_after_the_run_started_credits_nobody(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    routine_id = await _routine(db_session_factory, person)
    token = await _token(
        db_session_factory,
        person,
        RelayBinding("Agent A", routine_id=routine_id, owner="user-1"),
    )
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, _tool_call())
        async with db_session_factory() as db, db.begin():
            await db.execute(
                text("UPDATE routines SET created_by_user_id = 'user-2' WHERE id = :id"),
                {"id": routine_id},
            )
        await _post(client, token, _tool_call())
    first, second = (_sent_meta(r) for r in upstream.requests)
    assert first["requester_platform_user_id"] == "user-1", "the owner the run started for"
    assert second == {}, "the new owner is not credited with the old run's calls"


@pytest.mark.parametrize("human", [None, False])
@pytest.mark.parametrize("dm", [False, True])
async def test_an_unattended_turn_is_stamped_as_a_routine_not_a_person(
    db_session_factory: async_sessionmaker[AsyncSession], human: bool | None, dm: bool
) -> None:
    """A wake (watch, timer, continuation) or a DM routine has an origin that no
    person started: it names the person it acts for, with kind "routine"."""
    person = await _person(db_session_factory)
    thread = "dm:scope-1" if dm else "thread-1"
    token, _ = await _chat_session(db_session_factory, person, thread, human=human, dm=dm)
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, _tool_call(requester_kind="person"))
    meta = _sent_meta(upstream.requests[0])
    assert meta["requester_kind"] == "routine", "only a person's own message is kind person"
    assert meta["requester_platform_user_id"] == "user-1"
    assert meta["requester_conversation"] == (
        {"place_type": "dm", "channel_id": None, "parent_channel_id": None}
        if dm
        else {"place_type": "thread", "channel_id": "thread-1", "parent_channel_id": "chan-1"}
    )


async def test_two_live_turns_and_a_replaced_session_stamp_no_requester(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, mapping_id = await _chat_session(db_session_factory, person, "thread-1")
    await _chat_session(db_session_factory, person, "thread-1")  # a second turn in the thread
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, _tool_call(requester_platform_user_id="666"))
        async with db_session_factory() as db, db.begin():
            await mark_dead(db, id=mapping_id)
        await _post(client, token, _tool_call(requester_platform_user_id="666"))
    assert [_sent_meta(r) for r in upstream.requests] == [{}, {}], (
        "ambiguous or stale turns name nobody"
    )


async def test_concurrent_turns_by_two_people_in_one_thread_each_name_their_asker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each call is stamped from the turn that holds its session's grant: a queued
    turn of the same person beside it does not make the call anonymous."""
    first = await _person(db_session_factory)
    second = await _colleague(db_session_factory, first)
    token_1, _ = await _chat_session(
        db_session_factory, first, "thread-1", grant="grant-1", message="msg-1"
    )
    token_2, _ = await _chat_session(
        db_session_factory, second, "thread-1", grant="grant-2", message="msg-2"
    )
    await _chat_session(db_session_factory, first, "thread-1", message="msg-queued")
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token_2, _tool_call(requester_platform_user_id="user-1"))
        await _post(client, token_1, _tool_call(requester_platform_user_id="user-2"))
    stamped = [_sent_meta(r).get("requester_platform_user_id") for r in upstream.requests]
    assert stamped == ["user-2", "user-1"], "each call names the person whose turn made it"
    signer, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, signer, key) as client:
        await _call(client, token_1)
    assert _identity(signer.requests[0], key)["msg"] == "msg-1", "the running turn, not the queued"


async def test_the_turn_holding_the_grant_is_named_until_it_lets_go(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session row replaced under a running turn still names that turn; once the
    turn releases the grant, nobody is named, and a grant never names another account."""
    person = await _person(db_session_factory)
    token, mapping_id = await _chat_session(db_session_factory, person, "thread-1", grant="g-1")
    other = await _colleague(db_session_factory, person)
    # A credential of another account naming the first person's grant.
    borrowed = await _token(
        db_session_factory, other, RelayBinding("Agent A", mapping_id, grant="g-1")
    )
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        async with db_session_factory() as db, db.begin():
            await mark_dead(db, id=mapping_id)
        await _post(client, token, _tool_call())
        await _post(client, borrowed, _tool_call())
        async with db_session_factory() as db, db.begin():
            origin_id = (
                await db.execute(text("SELECT id FROM turn_origins WHERE chat_session_id = 'g-1'"))
            ).scalar_one()
            await bind_origin_session(db, origin_id=origin_id, chat_session_id=None)
        await _post(client, token, _tool_call(requester_platform_user_id="user-1"))
    held, borrowed_meta, released = (_sent_meta(r) for r in upstream.requests)
    assert held["requester_platform_user_id"] == "user-1", "the turn holding the grant is named"
    assert borrowed_meta == {}, "a grant names only its own account's turn"
    assert released == {}, "a released grant names nobody, and a forged field is dropped"


async def test_a_platform_the_server_does_not_understand_is_never_stamped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, platform="slack", workspace_id="T1")
        principal = await make_platform_principal(
            db, platform="slack", external_id="U1", tenant=tenant
        )
    person = _Person(
        tenant.id,
        principal.account_id,
        derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_a"),
    )
    mapping_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    async with db_session_factory() as db, db.begin():
        await create_thread_session(
            db, id=mapping_id, tenant_id=tenant.id, platform="slack", thread_id="t1",
            account_id=person.account_id, ma_session_id="sess_t1", ma_agent_id="agent_a",
        )  # fmt: skip
        await create_origin(
            db, tenant_id=tenant.id, account_id=person.account_id, platform="slack",
            parent_channel_id="C1", thread_id="t1", responder_ma_agent_id="agent_a",
            responder_name="Helper", configuration_target_ma_agent_id=None,
            configuration_target_name=None, role=Role.USER,
            expires_at=now + dt.timedelta(hours=1), now=now, sender_foreign=False,
            shared_channel=False, platform_channel_id="C1", platform_message_id="m1",
        )  # fmt: skip
    token = await _token(db_session_factory, person, RelayBinding("Agent A", mapping_id))
    server = BEARER_SERVER.model_copy(update={"workspaces": frozenset({"slack:T1"})})
    upstream = _Upstream()
    async with _relay(db_session_factory, upstream, None, server, bearers=_BEARERS) as client:
        await _post(client, token, _tool_call(requester_platform_user_id="666"))
    assert _sent_meta(upstream.requests[0]) == {}, "only platforms the field is defined for"


async def test_batches_and_other_methods_follow_the_same_rule(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    batch = json.dumps([json.loads(_tool_call(requester_platform_user_id="666")), ping])
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, batch)
        get = await client.get("/mcp-identity/tools", headers={"authorization": f"Bearer {token}"})
        deleted = await client.delete(
            "/mcp-identity/tools", headers={"authorization": f"Bearer {token}"}
        )
    sent = json.loads(upstream.requests[0].content)
    assert sent[0]["params"]["_meta"]["requester_platform_user_id"] == "user-1", "batch call"
    assert sent[1] == ping, "a non-call is untouched"
    assert (get.status_code, deleted.status_code) == (200, 200), "GET and DELETE pass through"
    assert [r.method for r in upstream.requests] == ["POST", "GET", "DELETE"]
    assert all(r.headers["authorization"] == f"Bearer {BEARER_KEY}" for r in upstream.requests), (
        "every method is sent with the key"
    )
    assert upstream.requests[1].content == b"", "a GET body is not rewritten"


async def test_forged_requester_fields_on_any_method_are_stripped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    forged = {"requester_platform_user_id": "666", "requester_kind": "person", "keep": "me"}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"_meta": forged}},
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"_meta": dict(forged)}},
    ]
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        await _post(client, token, json.dumps(messages))
        deleted = await client.request(
            "DELETE",
            "/mcp-identity/tools",
            content=_tool_call(requester_platform_user_id="666"),
            headers={"authorization": f"Bearer {token}"},
        )
    sent = json.loads(upstream.requests[0].content)
    assert [m["params"]["_meta"] for m in sent] == [{"keep": "me"}] * 2, "only the forged keys go"
    assert deleted.status_code == 200
    assert upstream.requests[1].content == b"", "a DELETE carries no body upstream"


async def test_a_body_that_cannot_be_re_encoded_is_a_bad_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    surrogate = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\ud800"}}'
    deep = b"[" * 50_000 + b"]" * 50_000
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        statuses = [(await _post(client, token, body)).status_code for body in (surrogate, deep)]
    assert statuses == [400, 400], "a lone surrogate or runaway nesting is refused, not a 500"
    assert upstream.requests == [], "nothing was forwarded"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"method":"tools/call","params":{"_meta":{"requester_platform_user_id":"1",'
        b'"requester_platform_user_id":"2"}}}',
        b'{"method":"tools/call","params":"x"}',
    ],
)
async def test_unreadable_call_bodies_are_refused_not_forwarded(
    db_session_factory: async_sessionmaker[AsyncSession], body: bytes
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    async with _relay(
        db_session_factory, upstream, None, BEARER_SERVER, bearers=_BEARERS
    ) as client:
        response = await _post(client, token, body)
    assert response.status_code == 400, "a body that cannot be stamped is a 400"
    assert upstream.requests == [], "nothing reaches the server unchecked"


async def test_an_agent_outside_the_allowlist_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    server = BEARER_SERVER.model_copy(update={"agents": ("Someone Else",)})
    upstream = _Upstream()
    async with _relay(db_session_factory, upstream, None, server, bearers=_BEARERS) as client:
        refused = await _post(client, token, _tool_call())
    assert refused.status_code == 403, "an agent the server does not list is refused"
    assert upstream.requests == [], "the key is never sent for it"


async def test_a_bearer_server_without_its_key_fails_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    async with _relay(db_session_factory, upstream, None, BEARER_SERVER) as client:
        response = await _post(client, token, _tool_call())
    assert response.status_code == 503, "no key means no request, not an unauthenticated one"
    assert upstream.requests == [], "nothing was forwarded"


async def test_a_signed_server_does_not_stamp_unless_asked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    upstream = _Upstream()
    body = _tool_call(requester_platform_user_id="666")
    async with _relay(db_session_factory, upstream, generate_signing_key()) as client:
        await _post(client, token, body)
    assert upstream.requests[0].content == body.encode(), "bodies pass byte for byte by default"


@pytest.mark.parametrize(
    ("signed", "listed", "managed", "relayed"),
    [
        ("default", "daimon", True, True),
        ("daimon", "default", True, True),
        ("default", "daimon", False, False),
    ],
    ids=["renamed-seeded", "old-credential", "own-default"],
)
async def test_an_allowlist_naming_the_builtin_when_its_name_changed_then_admits_only_seeded(
    signed: str,
    listed: str,
    managed: bool,
    relayed: bool,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token = await _token(db_session_factory, person, RelayBinding(signed))
    agent = ma_agent(
        id="agent_a",
        name="default",
        tenant_id=person.tenant_id,
        metadata={"daimon_managed": "true"} if managed else None,
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent.model_dump(mode="json")]))
    server = BEARER_SERVER.model_copy(update={"agents": (listed,)})
    upstream = _Upstream()
    async with _relay(
        db_session_factory,
        upstream,
        None,
        server,
        bearers=_BEARERS,
        anthropic=build_fake_anthropic(router.dispatch),
    ) as client:
        response = await _post(client, token, _tool_call())
    assert (response.status_code != 403) is relayed, "the rename keeps the seeded agent listed"
    assert bool(upstream.requests) is relayed, "a workspace's own namesake is never relayed"


@pytest.mark.parametrize(
    ("mode", "contained", "relay", "status"),
    [
        ("off", True, False, 200),
        ("shadow", True, False, 200),
        ("on", True, False, 403),
        ("on", True, True, 403),
        ("on", False, True, 200),
        ("on", False, False, 200),
    ],
    ids=str,
)
async def test_a_held_token_is_refused_only_for_a_contained_room_in_audience_mode_on(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    contained: bool,
    relay: bool,
    status: int,
) -> None:
    """A room outsiders may read has no identity server in `on`, so a leftover held token gets
    no answer. A relay-only hold of an internal room keeps working."""
    from daimon.core.channel_audience import ChannelAudience

    person = await _person(db_session_factory)
    _, mapping_id = await _chat_session(db_session_factory, person, "thread-9")
    token = await _token(
        db_session_factory,
        person,
        RelayBinding("Agent A", mapping_id),
        TurnHold("discord", "chan-1", client=False, relay=relay),
    )

    async def read(*_: object, **__: object) -> str:
        return mode

    async def audience(*_: object, **__: object) -> ChannelAudience:
        return ChannelAudience(
            contained=contained, known=not contained, narrow=False, source="facts"
        )

    monkeypatch.setattr("daimon.adapters.mcp.identity_relay.read_audience_mode", read)
    monkeypatch.setattr("daimon.adapters.mcp.identity_relay.channel_has_outsiders", audience)
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        response = await _call(client, token)
    assert response.status_code == status
    assert len(upstream.requests) == (1 if status == 200 else 0)


async def test_a_held_token_with_an_unreadable_room_is_refused_in_mode_on(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    person = await _person(db_session_factory)
    _, mapping_id = await _chat_session(db_session_factory, person, "thread-9")
    token = await _token(
        db_session_factory,
        person,
        RelayBinding("Agent A", mapping_id),
        TurnHold("discord", "chan-1", client=False, relay=True),
    )

    async def read(*_: object, **__: object) -> str:
        return "on"

    async def broken(*_: object, **__: object) -> object:
        raise RuntimeError("database down")

    monkeypatch.setattr("daimon.adapters.mcp.identity_relay.read_audience_mode", read)
    monkeypatch.setattr("daimon.adapters.mcp.identity_relay.channel_has_outsiders", broken)
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(db_session_factory, upstream, key) as client:
        assert (await _call(client, token)).status_code == 403


AGENT_CHAT_SERVER = WAKE_SERVER.model_copy(update={"agent_chat_requester": True})
_FORGED = _tool_call(
    requester_platform_user_id="666",
    requester_conversation={"place_type": "dm", "channel_id": "evil"},
    requester_kind="person",
    wake_url="https://evil.example.com",
)


async def _agent_chat(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    *,
    signed_in: bool | None,
    session_id: str = "sesn_chat",
) -> str:
    """An agent-chat session's relay credential; `signed_in` is its recorded driver."""
    if signed_in is not None:
        await record_driver(
            factory,
            managed_session_id=session_id,
            signed_in_person=signed_in,
            session_status=None,
            earlier_turn_open=False,
        )
    return await _token(factory, person, RelayBinding("Agent A", agent_chat=session_id))


async def _agent_chat_meta(
    factory: async_sessionmaker[AsyncSession], token: str, server: SignedIdentityServer
) -> dict[str, object]:
    upstream = _Upstream()
    async with _relay(factory, upstream, None, server, bearers=_BEARERS) as client:
        response = await _post(client, token, _FORGED)
    assert response.status_code == 200, "the call runs whoever drives it"
    return _sent_meta(upstream.requests[0])


async def test_an_agent_chat_turn_a_signed_in_person_drives_names_them_with_no_place(
    db_session_factory: async_sessionmaker[AsyncSession], callbacks_on: None
) -> None:
    person = await _person(db_session_factory)
    token = await _agent_chat(db_session_factory, person, signed_in=True)
    assert await _agent_chat_meta(db_session_factory, token, AGENT_CHAT_SERVER) == {
        "requester_platform_user_id": "user-1",
        "requester_kind": "agent_chat",
    }, "the signed-in person is named from their account, never from the request"
    assert await _wake_watches(db_session_factory) == [], "and no wake link is made"


@pytest.mark.parametrize(
    ("signed_in", "server", "case"),
    [
        (True, WAKE_SERVER, "a server that has not opted in"),
        (False, AGENT_CHAT_SERVER, "a turn an agent, coding or CLI key drives"),
        (None, AGENT_CHAT_SERVER, "a session with no recorded driver"),
    ],
)
async def test_an_agent_chat_turn_otherwise_names_nobody(
    db_session_factory: async_sessionmaker[AsyncSession],
    signed_in: bool | None,
    server: SignedIdentityServer,
    case: str,
) -> None:
    person = await _person(db_session_factory)
    token = await _agent_chat(db_session_factory, person, signed_in=signed_in)
    assert await _agent_chat_meta(db_session_factory, token, server) == {}, (
        f"{case}: forged requester fields are only removed"
    )


async def test_a_credential_minted_before_the_agent_chat_binding_names_nobody(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Such a session gains the binding on its next continue (`refresh_relay_vault`); until
    then the relay cannot tell which session calls, so it names nobody."""
    person = await _person(db_session_factory)
    await _agent_chat(db_session_factory, person, signed_in=True)
    token = await _token(db_session_factory, person, RelayBinding("Agent A"))
    assert await _agent_chat_meta(db_session_factory, token, AGENT_CHAT_SERVER) == {}
