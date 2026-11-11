"""The connector relay: a provider turn's MCP connectors, called by Daimon, on real Postgres.

The provider sends the turn's own Daimon credential to `/connectors/{key}`; Daimon
scopes the key to the turn's tenant and agent, gates the call, and calls the
connector with the person's token, which the provider never sees.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.verifier import CHAT_EXECUTION_CLAIM, TOKEN_JTI_CLAIM
from daimon.adapters.mcp.connector_relay import (
    CONNECTOR_ROUTE,
    FAILED_TEXT,
    RECONNECT_TEXT,
    REFUSED_TEXT,
    ConnectorRelay,
    RelayTurn,
    relay_token,
    relay_turn,
)
from daimon.core import host_calls
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.connector_relay import (
    Connector,
    ToolsCache,
    connectors_of,
    daimon_destination,
    relay_key,
    relay_url,
    relay_verdict,
)
from daimon.core.github_credentials import encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import HOLD_CLAIM
from daimon.core.mcp_tool_call import (
    INTERACTIVE_LIMITS,
    McpCallError,
    McpToolSession,
    mcp_tool_session,
    pinned_http_client,
)
from daimon.core.stores import connector_tokens as store
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.host_call_cards import HostCallCardRow, list_cards
from daimon.core.stores.mcp_tokens import create_mcp_token_row, get_mcp_token
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from pydantic import HttpUrl, JsonValue, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

_SECRET = "s" * 32
_PUBLIC = "https://daimon.example.com/mcp"
_UP = "https://tools.example.com/mcp"
_TOKEN_URL = "https://auth.example.com/token"
_PUBLIC_IP = "93.184.216.34"
_USER = "user-1"
_MA_AGENT = "agent_x"
_MESSAGE = "msg-1"
_KEY = "notes"


def _settings(**features: str) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr(_SECRET), public_url=HttpUrl(_PUBLIC)),
        features=features,
    )


async def _public(_host: str, _port: int) -> str:
    return _PUBLIC_IP


@dataclass
class Upstream:
    """A connector's MCP server: what it lists, what it was asked, with which token."""

    tools: list[dict[str, JsonValue]] = field(
        default_factory=lambda: [
            {"name": "read", "inputSchema": {"type": "object"}},
            {
                "name": "write",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        ]
    )
    lists: int = 0
    calls: list[str] = field(default_factory=list[str])
    tokens: set[str] = field(default_factory=set[str])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.tokens.add(request.headers.get("authorization", ""))
        message = json.loads(request.content)
        method, rid = message["method"], message.get("id")
        if rid is None:
            return httpx.Response(202)
        result: dict[str, Any] = {}
        if method == "tools/list":
            self.lists += 1
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.calls.append(message["params"]["name"])
            result = {"content": [{"type": "text", "text": "done"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})

    def open(self, url: str, token: str) -> AbstractAsyncContextManager[McpToolSession]:
        return mcp_tool_session(
            url,
            token,
            resolve=_public,
            transport=httpx.MockTransport(self.handle),
            limits=INTERACTIVE_LIMITS,
        )


class _Verifier(TokenVerifier):
    """Each bearer names a fixed claim set, as the real verifier returns it."""

    def __init__(self) -> None:
        super().__init__()
        self.claims: dict[str, dict[str, Any]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = self.claims.get(token)
        return (
            None
            if claims is None
            else AccessToken(token=token, client_id="c", scopes=[], claims=claims)
        )


@dataclass
class Rig:
    """One thread on a provider turn, with its relay and everything it touched."""

    sessions: async_sessionmaker[AsyncSession]
    tenant: TenantRow
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    grant: str
    verifier: _Verifier
    upstream: Upstream
    connectors: dict[str, Connector]
    token: list[str | None]
    posted: list[HostCallCardRow]
    loads: list[tuple[uuid.UUID, uuid.UUID]]
    features: dict[str, str] = field(default_factory=dict[str, str])

    def claims(self, **changes: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "sub": str(self.account_id),
            "tenant_id": str(self.tenant_id),
            "chat_agent_id": str(self.agent_id),
            "chat_session_id": self.grant,
            "platform": "discord",
            CHAT_EXECUTION_CLAIM: True,
            "no_confirmation_surface": True,
        }
        return {**base, **changes}

    def relay(self) -> ConnectorRelay:
        async def load(turn: RelayTurn, _origin: object) -> Mapping[str, Connector]:
            self.loads.append((turn.tenant_id, turn.agent_id))
            return self.connectors

        async def token_for(_turn: RelayTurn, _connector: Connector) -> str | None:
            return self.token[0]

        async def post_card(_turn: RelayTurn, _origin: object, row: HostCallCardRow) -> bool:
            self.posted.append(row)

            async def post(_card: object) -> str:
                return _MESSAGE

            return await host_calls.post_call_card(self.sessions, row, post=post)

        return ConnectorRelay(
            auth=self.verifier,
            settings=_settings(**self.features),
            sessionmaker=self.sessions,
            public_url=_PUBLIC,
            load_connectors=load,
            token_for=token_for,
            post_card=post_card,
            open_session=self.upstream.open,
        )

    async def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        claims: dict[str, Any] | None = None,
        key: str = _KEY,
        relay: ConnectorRelay | None = None,
    ) -> httpx.Response:
        bearer = f"t-{uuid.uuid4()}"
        self.verifier.claims[bearer] = claims if claims is not None else self.claims()
        handler = (relay or self.relay()).handle
        app = Starlette(routes=[Route(CONNECTOR_ROUTE, handler, methods=["GET", "POST"])])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://daimon.example.com"
        ) as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            return await client.post(
                f"/connectors/{key}", json=body, headers={"authorization": f"Bearer {bearer}"}
            )


async def _make_rig(sessions: async_sessionmaker[AsyncSession], *, attended: bool) -> Rig:
    now = dt.datetime.now(dt.UTC)
    grant = f"grant-{uuid.uuid4()}"
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="guild-1")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id=_USER, tenant=tenant, account=account
        )
        await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="channel-1",
            thread_id="thread-1",
            responder_ma_agent_id=_MA_AGENT,
            responder_name="Researcher",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(hours=1),
            now=now,
            sender_foreign=False,
            shared_channel=False,
            chat_session_id=grant,
            attended=attended,
        )
    return Rig(
        sessions=sessions,
        tenant=tenant,
        tenant_id=tenant.id,
        account_id=account.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT),
        grant=grant,
        verifier=_Verifier(),
        upstream=Upstream(),
        connectors={_KEY: Connector(_KEY, _UP, "ask", {"read": "auto", "write": "ask"})},
        token=["person-token"],
        posted=[],
        loads=[],
    )


@pytest.fixture
async def rig(committing_sessionmaker: async_sessionmaker[AsyncSession]) -> Rig:
    return await _make_rig(committing_sessionmaker, attended=True)


def _result(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, f"a JSON-RPC answer: {response.status_code}"
    body = response.json()
    assert "result" in body, f"a result, not an error: {body}"
    return cast("dict[str, Any]", body["result"])


def _text(result: Mapping[str, Any]) -> str:
    return "".join(part.get("text", "") for part in result.get("content", []))


async def _cards(rig: Rig) -> list[HostCallCardRow]:
    async with rig.sessions() as session:
        return await list_cards(session, tenant_id=rig.tenant_id)


# --- the pure part -----------------------------------------------------------------


def test_relay_urls_round_trip_and_only_name_daimons_own_server() -> None:
    """A relay URL names its key; only Daimon's own URLs resolve as Daimon destinations."""
    url = relay_url(_PUBLIC, _KEY)
    assert url == f"{_PUBLIC}/connectors/{_KEY}", f"the relay sits under the MCP URL: {url}"
    assert relay_key(url) == _KEY, "the key is read back from the URL"
    assert relay_key(_UP) is None, "a connector URL is no relay URL"
    assert daimon_destination(url, _PUBLIC) == _PUBLIC, "a relay URL resolves Daimon's credential"
    assert daimon_destination(_PUBLIC, _PUBLIC) == _PUBLIC, "Daimon's own server still does"
    elsewhere = relay_url("https://evil.example.com/mcp", _KEY)
    assert daimon_destination(elsewhere, _PUBLIC) is None, "another host's relay path is refused"


def test_connectors_of_reads_the_agents_own_permissions() -> None:
    """Each user connector with a toolset is relayed with its permission; Daimon's is not."""
    spec = AgentSpec(
        name="a",
        model=ModelRef(provider="anthropic", id="m"),
        tools=(
            ToolSpec(name=_KEY, kind="mcp_toolset", permission="ask"),
            ToolSpec(name=DAIMON_SERVER_NAME, kind="mcp_toolset"),
        ),
        mcp_servers=(
            MCPConnection(name=_KEY, url=_UP),
            MCPConnection(name=DAIMON_SERVER_NAME, url=_PUBLIC),
            MCPConnection(name="bare", url="https://bare.example.com/mcp"),
        ),
    )
    found = connectors_of(spec)
    assert set(found) == {_KEY}, f"only the toolset-backed user connector: {set(found)}"
    assert found[_KEY].permission("anything") == "ask", "the toolset permission applies"


async def test_relay_verdict_cards_ask_and_refuses_it_unattended() -> None:
    """always_ask goes to press-applies; an unattended turn has nobody to press."""
    policy = _settings().tool_safety
    connector = Connector(_KEY, _UP, "ask", {"off": "disabled"})
    call = ToolCall(tool_use_id="t", server_name=_KEY, tool_name="write", input={})
    assert await relay_verdict(policy, connector, call, attended=True) == "ask", "a card"
    assert await relay_verdict(policy, connector, call, attended=False) == "refuse", "unattended"
    off = ToolCall(tool_use_id="t", server_name=_KEY, tool_name="off", input={})
    assert await relay_verdict(policy, connector, off, attended=True) == "refuse", "disabled"


def test_tools_cache_is_keyed_by_requester_and_token() -> None:
    """One person's catalogue never lists for another; a new credential drops the entry."""
    now = [0.0]
    cache = ToolsCache(clock=lambda: now[0])
    tenant, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tools: tuple[dict[str, JsonValue], ...] = ({"name": "read"},)
    cache.put(tenant, alice, _UP, "a-token", tools)
    assert cache.get(tenant, alice, _UP, "a-token") == tools, "the owner reads it back"
    assert cache.get(tenant, bob, _UP, "a-token") is None, "another requester never does"
    assert cache.get(uuid.uuid4(), alice, _UP, "a-token") is None, "nor another tenant"
    assert cache.get(tenant, alice, _UP, "rotated") is None, "a changed token misses"
    assert cache.get(tenant, alice, _UP, "a-token") is None, "and dropped the old entry"
    cache.put(tenant, alice, _UP, "a-token", tools)
    now[0] = 301.0
    assert cache.get(tenant, alice, _UP, "a-token") is None, "entries expire after 5 minutes"


# --- the upstream client ---------------------------------------------------------


async def test_upstream_refuses_private_addresses_and_redirects() -> None:
    """The connector call is pinned to a public address and never follows a redirect."""

    async def private(_host: str, _port: int) -> str:
        raise McpCallError("address")

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    with pytest.raises(McpCallError) as refused:
        async with mcp_tool_session(_UP, "t", resolve=private, limits=INTERACTIVE_LIMITS):
            pass
    assert refused.value.kind == "address", f"a private address is refused: {refused.value.kind}"
    with pytest.raises(McpCallError) as moved:
        async with mcp_tool_session(
            _UP, "t", resolve=_public, transport=httpx.MockTransport(redirect)
        ):
            pass
    assert moved.value.kind == "redirect", f"a redirect is refused: {moved.value.kind}"
    with pytest.raises(McpCallError) as plain:
        async with mcp_tool_session("http://tools.example.com/mcp", "t", resolve=_public):
            pass
    assert plain.value.kind == "address", "only https is called"


# --- the route -------------------------------------------------------------------


async def test_tools_list_is_cached_until_the_token_changes(rig: Rig) -> None:
    """A second list reads the cache; a rotated token lists upstream again."""
    relay = rig.relay()
    first = _result(await rig.send("tools/list", relay=relay))
    await rig.send("tools/list", relay=relay)
    assert rig.upstream.lists == 1, f"the second list is cached: {rig.upstream.lists}"
    assert [t["name"] for t in first["tools"]] == ["read", "write"], "the connector's tools"
    rig.token[0] = "rotated"
    await rig.send("tools/list", relay=relay)
    assert rig.upstream.lists == 2, "a changed credential drops the cached list"
    assert rig.upstream.tokens == {"Bearer person-token", "Bearer rotated"}, (
        "the connector sees the person's token, never the turn credential"
    )


async def test_an_unknown_tool_lists_once_more_then_refuses(rig: Rig) -> None:
    """A name missing from the list may be new: one fresh list, then a refusal."""
    relay = rig.relay()
    await rig.send("tools/list", relay=relay)
    result = _result(await rig.send("tools/call", {"name": "nope", "arguments": {}}, relay=relay))
    assert _text(result) == REFUSED_TEXT, f"an unknown tool is refused: {result}"
    assert rig.upstream.lists == 2, f"exactly one more list: {rig.upstream.lists}"
    assert rig.upstream.calls == [], "nothing is called upstream"


async def test_always_ask_records_a_card_bound_to_the_relay_and_the_server(rig: Rig) -> None:
    """An ask tool is carded, not run; its digest names both the route and the connector."""
    args = {"text": "hi"}
    result = _result(await rig.send("tools/call", {"name": "write", "arguments": args}))
    assert rig.upstream.calls == [], "an ask tool never runs before the press"
    assert "content" in result and len(rig.posted) == 1, f"the card was posted: {result}"
    [card] = await _cards(rig)
    expected = host_calls.canonical_call_digest(
        route=host_calls.ROUTE_CONNECTOR,
        server=f"{_PUBLIC}/connectors/{_KEY}|{_UP}",
        tool="write",
        args=args,
        executing_agent_id=rig.agent_id,
    )
    assert card.args_digest == expected, "the digest binds the relay route and the server url"
    assert card.reference["server_key"] == _KEY, "the wake runs it through the same relay"


async def test_an_approved_card_runs_once_under_its_one_shot_credential(rig: Rig) -> None:
    """The wake's confirmed credential runs exactly the carded call, then is revoked."""
    args = {"text": "hi"}
    await rig.send("tools/call", {"name": "write", "arguments": args})
    [card] = await _cards(rig)

    async def admin(_: str) -> bool:
        return True

    pressed = await host_calls.press_host_call(
        sessionmaker=rig.sessions,
        enabled=True,
        tenant_id=rig.tenant_id,
        card_id=card.id,
        action="approve",
        clicker=_USER,
        message_id=_MESSAGE,
        current_admin=admin,
        now=dt.datetime.now(dt.UTC),
        queue_wake=False,
    )
    assert pressed is not None and pressed.approved, f"the requester approves: {pressed}"
    jti = uuid.uuid4()
    async with rig.sessions.begin() as session:
        await create_mcp_token_row(
            session,
            jti=jti,
            account_id=rig.account_id,
            tenant_id=rig.tenant_id,
            agent_id=str(rig.agent_id),
            label=None,
            created_at=dt.datetime.now(dt.UTC),
        )
    confirmed = rig.claims(confirmed_call=str(card.id), **{TOKEN_JTI_CLAIM: str(jti)})
    other = {"name": "write", "arguments": {"text": "else"}}
    refused = _result(await rig.send("tools/call", other, claims=confirmed))
    assert _text(refused) == REFUSED_TEXT and rig.upstream.calls == [], "only the carded call"
    ran = _result(
        await rig.send("tools/call", {"name": "write", "arguments": args}, claims=confirmed)
    )
    assert _text(ran) == "done" and rig.upstream.calls == ["write"], f"it ran once: {ran}"
    async with rig.sessions() as session:
        row = await get_mcp_token(session, jti=jti)
    assert row is not None and row.revoked_at is not None, "the one-shot credential is revoked"


async def test_an_approved_card_never_runs_under_another_persons_credential(rig: Rig) -> None:
    """A confirmed credential of someone else leaves the card approved for its own person."""
    args = {"text": "hi"}
    await rig.send("tools/call", {"name": "write", "arguments": args})
    [card] = await _cards(rig)

    async def admin(_: str) -> bool:
        return True

    pressed = await host_calls.press_host_call(
        sessionmaker=rig.sessions,
        enabled=True,
        tenant_id=rig.tenant_id,
        card_id=card.id,
        action="approve",
        clicker=_USER,
        message_id=_MESSAGE,
        current_admin=admin,
        now=dt.datetime.now(dt.UTC),
        queue_wake=False,
    )
    assert pressed is not None and pressed.approved, f"the requester approves: {pressed}"
    async with rig.sessions.begin() as session:
        guest = await make_account(session, tenant=rig.tenant)
    confirmed = rig.claims(sub=str(guest.id), confirmed_call=str(card.id))
    refused = await rig.send("tools/call", {"name": "write", "arguments": args}, claims=confirmed)
    assert refused.status_code == 403 and rig.upstream.calls == [], f"refused: {refused}"
    [after] = await _cards(rig)
    assert after.status == "approved", f"the card still waits for its own wake: {after.status}"


async def test_always_ask_on_an_unattended_turn_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody can press a card on an unattended turn: the unattended verdict applies."""
    rig = await _make_rig(committing_sessionmaker, attended=False)
    result = _result(await rig.send("tools/call", {"name": "write", "arguments": {}}))
    assert _text(result) == REFUSED_TEXT, f"refused unattended: {result}"
    assert await _cards(rig) == [] and rig.upstream.calls == [], "no card, no call"


async def test_a_held_turn_sees_only_allowlisted_connectors(rig: Rig) -> None:
    """A held turn reaches no connector outside the allowlist, and no token is read."""
    rig.token[0] = None  # would answer Reconnect if the token were read
    body = (await rig.send("tools/list", claims=rig.claims(**{HOLD_CLAIM: "h"}))).json()
    assert body["error"]["message"] == REFUSED_TEXT, f"a held turn is refused: {body}"
    assert rig.upstream.lists == 0, "the connector is never reached"


async def test_a_missing_token_asks_the_person_to_reconnect(rig: Rig) -> None:
    """No usable sign-in counts as ungranted: the person gets the Reconnect sign-in."""
    rig.token[0] = None
    body = (await rig.send("tools/list")).json()
    assert body["error"]["message"] == RECONNECT_TEXT, f"reconnect: {body}"


async def test_a_failing_connector_answers_a_plain_failure(rig: Rig) -> None:
    """A redirecting connector is never followed; the model gets the plain failure."""
    rig.upstream.handle = lambda _request: httpx.Response(302, headers={"location": _PUBLIC})  # type: ignore[method-assign]
    body = (await rig.send("tools/list")).json()
    assert body["error"]["message"] == FAILED_TEXT, f"a plain failure: {body}"


async def test_server_keys_resolve_only_in_the_turns_tenant_and_agent(rig: Rig) -> None:
    """Another agent's or tenant's credential, an unknown key, or MA's own credential fail."""
    other_agent = rig.claims(chat_agent_id=str(uuid.uuid4()))
    assert (await rig.send("tools/list", claims=other_agent)).status_code == 403, "agent"
    other_tenant = rig.claims(tenant_id=str(uuid.uuid4()))
    assert (await rig.send("tools/list", claims=other_tenant)).status_code == 403, "tenant"
    assert (await rig.send("tools/list", key="other")).status_code == 404, "unknown key"
    # Managed Agents turns can show cards and call connectors natively: no relay.
    native = rig.claims(no_confirmation_surface=False)
    assert (await rig.send("tools/list", claims=native)).status_code == 403, "MA unchanged"
    malformed = rig.claims(requester_account_id="not-a-uuid")
    assert (await rig.send("tools/list", claims=malformed)).status_code == 403, "malformed"
    assert set(rig.loads) == {(rig.tenant_id, rig.agent_id)}, "only this agent's spec loads"


def _harness(rig: Rig, **changes: Any) -> dict[str, Any]:
    """A self-hosted harness's credential: no card surface, asks the person itself."""
    return rig.claims(no_confirmation_surface=False, anthropic_disabled=True, **changes)


async def test_a_self_hosted_harness_runs_its_own_asks_without_a_card(rig: Rig) -> None:
    """The harness asked in-process before the call: Daimon runs it, posts no card."""
    result = _result(
        await rig.send("tools/call", {"name": "write", "arguments": {}}, claims=_harness(rig))
    )
    assert _text(result) == "done" and rig.upstream.calls == ["write"], f"ran: {result}"
    assert await _cards(rig) == [] and rig.posted == [], "no card for a harness ask"
    confirmed = _harness(rig, confirmed_call=str(uuid.uuid4()))
    assert (await rig.send("tools/list", claims=confirmed)).status_code == 403, "no pressed call"


async def test_a_self_hosted_harness_ask_is_refused_unattended(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody is there to answer the harness: the unattended refusal still applies."""
    rig = await _make_rig(committing_sessionmaker, attended=False)
    sent = await rig.send("tools/call", {"name": "write", "arguments": {}}, claims=_harness(rig))
    assert _text(_result(sent)) == REFUSED_TEXT and rig.upstream.calls == [], "refused"


async def test_a_self_hosted_record_serves_only_its_daimon_signed_connectors(rig: Rig) -> None:
    """A record's relay connectors skip the operator's own credentials, fit the credential
    kind and the record's own identity; Daimon refreshes its sign-ins."""
    from daimon.adapters.mcp.connector_relay import record_connectors
    from daimon.core.mcp_oauth.connector_tokens import agent_refresh_owner
    from daimon.core.mux_backend import resource_scope
    from daimon.core.stores.domain import TurnOriginRow
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents

    agent = await SelfHostedAgents(rig.sessions).create(
        resource_scope(tenant_id=str(rig.tenant_id), account_id=str(rig.account_id)),
        AgentSpec(
            name="harness",
            model=ModelRef(provider="selfhosted", id="m"),
            tools=(
                ToolSpec(name=_KEY, kind="mcp_toolset", permission="ask"),
                ToolSpec(name="bound", kind="mcp_toolset"),
            ),
            mcp_servers=(
                MCPConnection(name=_KEY, url=_UP),
                MCPConnection(
                    name="bound",
                    url="https://bound.example/mcp",
                    credential_ref="HARNESS_MCP_CRED_B",
                ),
            ),
        ),
        key="harness",
    )
    origin = cast("TurnOriginRow", type("Origin", (), {"responder_ma_agent_id": agent.ref.id}))
    own = derive_agent_uuid(tenant_id=rig.tenant_id, ma_agent_id=agent.ref.id)
    turn = replace(_turn(rig, None), agent_id=own, carded=False)
    served = await record_connectors(rig.sessions, turn, origin)
    assert set(served) == {_KEY}, f"Daimon-signed connectors only: {set(served)}"
    assert served[_KEY].permission("write") == "ask", "the harness's own permission applies"
    carded = replace(turn, carded=True)
    assert await record_connectors(rig.sessions, carded, origin) == {}, "a card turn is not its own"
    other = replace(turn, agent_id=rig.agent_id)
    assert await record_connectors(rig.sessions, other, origin) == {}, "another identity gets none"
    async with rig.sessions() as session:
        owner = await agent_refresh_owner(
            session, tenant_id=rig.tenant_id, agent_id=own, selection_enabled=False
        )
        legacy = await agent_refresh_owner(
            session, tenant_id=rig.tenant_id, agent_id=rig.agent_id, selection_enabled=False
        )
    assert (owner, legacy) == ("daimon", "ma"), "a record has no vault; MA keeps its own"


async def test_turning_the_feature_off_closes_the_route(rig: Rig) -> None:
    """The rollback switch: off, the relay answers nothing."""
    rig.features = {"mcp_connector_relay": "off"}
    assert (await rig.send("tools/list")).status_code == 404, "off closes the route"
    assert rig.upstream.lists == 0, "and reaches no connector"


# --- tokens ------------------------------------------------------------------------


def _turn(rig: Rig, requester: uuid.UUID | None) -> RelayTurn:
    return RelayTurn(
        tenant_id=rig.tenant_id,
        account_id=rig.account_id,
        agent_id=rig.agent_id,
        chat_session_id=rig.grant,
        platform="discord",
        requester_account_id=requester,
        held=False,
        confirmed_call_id=None,
        token_jti=None,
    )


async def _seed_token(
    rig: Rig,
    fernet: MultiFernet,
    *,
    account_id: uuid.UUID | None,
    access: str,
    expires_in: dt.timedelta = dt.timedelta(hours=1),
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with rig.sessions.begin() as session:
        await store.upsert_connector_token(
            session,
            tenant_id=rig.tenant_id,
            account_id=account_id,
            agent_id=rig.agent_id,
            server_url=_UP,
            access_token_encrypted=encrypt_token(fernet, access),
            refresh_token_encrypted=encrypt_token(fernet, "rt-1"),
            token_endpoint=_TOKEN_URL,
            client_id="cid",
            client_secret_encrypted=None,
            token_endpoint_auth_method="none",
            scope="read",
            resource=_UP,
            expires_at=now + expires_in,
            refresh_owner="daimon",
            now=now,
        )


async def test_a_shared_thread_sees_only_agent_wide_credentials(rig: Rig) -> None:
    """Someone else asking in a shared thread never borrows the owner's personal sign-in."""
    fernet = make_fernet()
    async with rig.sessions.begin() as session:
        guest = await make_account(session, tenant=rig.tenant)
    await _seed_token(rig, fernet, account_id=rig.account_id, access="owner-at")
    connector = rig.connectors[_KEY]
    shared = _turn(rig, guest.id)
    assert shared.shared, "a requester other than the owner is a shared thread"
    assert await relay_token(rig.sessions, fernet, shared, connector) is None, (
        "the owner's personal token is hidden from the guest"
    )
    await _seed_token(rig, fernet, account_id=None, access="agent-at")
    assert await relay_token(rig.sessions, fernet, shared, connector) == "agent-at", (
        "the agent-wide sign-in is what a shared thread uses"
    )
    own = await relay_token(rig.sessions, fernet, _turn(rig, None), connector)
    assert own == "owner-at", f"the owner's own turn uses their personal sign-in: {own}"


async def test_a_shared_threads_credential_hides_even_the_askers_personal_sign_in(
    rig: Rig,
) -> None:
    """A shared thread's provider credential signs its asker, so personal sign-ins stay hidden."""
    fernet = make_fernet()
    await _seed_token(rig, fernet, account_id=rig.account_id, access="owner-at")
    turn = relay_turn(rig.claims(requester_account_id=str(rig.account_id)))
    assert turn is not None and turn.shared, f"the signed asker marks a shared thread: {turn}"
    token = await relay_token(rig.sessions, fernet, turn, rig.connectors[_KEY])
    assert token is None, f"no personal sign-in reaches a shared thread: {token}"


async def test_concurrent_relay_calls_refresh_the_token_once(rig: Rig) -> None:
    """Calls racing on an expiring sign-in spend its refresh token once."""
    fernet = make_fernet()
    await _seed_token(
        rig, fernet, account_id=None, access="old-at", expires_in=dt.timedelta(seconds=10)
    )
    forms: list[dict[str, list[str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        forms.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0.2)  # hold the claim while the others arrive
        return httpx.Response(
            200, json={"access_token": "new-at", "refresh_token": "rt-2", "expires_in": 3600}
        )

    def http() -> httpx.AsyncClient:
        return pinned_http_client(resolve=_public, transport=httpx.MockTransport(handler))

    turn = _turn(rig, None)
    tokens = await asyncio.gather(
        *(
            relay_token(rig.sessions, fernet, turn, rig.connectors[_KEY], http_factory=http)
            for _ in range(4)
        )
    )
    assert len(forms) == 1, f"one token-endpoint call: {len(forms)}"
    assert set(tokens) == {"new-at"}, f"every call gets the refreshed token: {tokens}"
