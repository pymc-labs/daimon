"""Daimon-owned connector tokens: one refresh per grant, bounded scope, pinned HTTP."""

from __future__ import annotations

import ast
import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.mcp_oauth.connector_tokens import (
    ConnectorScopeWidenedError,
    connector_access_token,
    connector_token_for_turn,
    refresh_connector_token,
    refresh_owner_for,
)
from daimon.core.mcp_oauth.discovery import discover_authorization_server
from daimon.core.mcp_oauth.flow import (
    McpOAuthTokenRefusedError,
    exchange_authorization_code,
    refresh_access_token,
)
from daimon.core.mcp_oauth.models import ClientRegistration
from daimon.core.mcp_personal_servers import hidden_mcp_server_names
from daimon.core.mcp_tool_call import (
    AddressRefusedError,
    pinned_http_client,
    resolve_public_address,
)
from daimon.core.stores import connector_tokens as store
from daimon.core.stores.domain import ConnectorTokenRow, McpOAuthGrantRow
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.config import ResolvedBackend
from mux.profiles import MANAGED_AGENTS
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_URL = "https://mcp.example.com/mcp"
_TOKEN_URL = "https://auth.example.com/token"
_PUBLIC_IP = "93.184.216.34"
_AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000b7")
_CLIENT = ClientRegistration(client_id="cid", client_secret=None, token_endpoint_auth_method="none")

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


async def _seed(
    session: AsyncSession,
    fernet: MultiFernet,
    *,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    expires_in: timedelta = timedelta(seconds=10),
) -> ConnectorTokenRow:
    now = datetime.now(UTC)
    row = await store.upsert_connector_token(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=_AGENT,
        server_url=_URL,
        access_token_encrypted=encrypt_token(fernet, "old-at"),
        refresh_token_encrypted=encrypt_token(fernet, "rt-1"),
        token_endpoint=_TOKEN_URL,
        client_id="cid",
        client_secret_encrypted=None,
        token_endpoint_auth_method="none",
        scope="read",
        resource=_URL,
        expires_at=now + expires_in,
        refresh_owner="daimon",
        now=now,
    )
    await session.commit()
    return row


def _pinned(handler: Handler, resolved: list[str] | None = None) -> Callable[[], httpx.AsyncClient]:
    async def resolve(host: str, _port: int) -> str:
        if resolved is not None:
            resolved.append(host)
        return _PUBLIC_IP

    return lambda: pinned_http_client(resolve=resolve, transport=httpx.MockTransport(handler))


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0.01)


async def test_concurrent_refreshes_make_one_token_call_and_rotate(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    # Real connections: the shared-connection factory would interleave the
    # callers' transactions on one connection.
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    fernet = make_fernet()
    row = await _seed(db_session, fernet, account_id=None, tenant_id=tenant.id)
    forms: list[dict[str, list[str]]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        forms.append(parse_qs(req.content.decode()))
        await asyncio.sleep(0.2)  # hold the claim while the others arrive
        return httpx.Response(
            200, json={"access_token": "new-at", "refresh_token": "rt-2", "expires_in": 3600}
        )

    results = await asyncio.gather(
        *(
            refresh_connector_token(
                factory,
                tenant_id=tenant.id,
                token_id=row.id,
                fernet=fernet,
                http_factory=_pinned(handler),
                sleep=_no_wait,
            )
            for _ in range(4)
        )
    )
    assert len(forms) == 1, "concurrent refreshes must spend the refresh token once"
    assert forms[0]["refresh_token"] == ["rt-1"] and forms[0]["scope"] == ["read"], (
        "the refresh asks for the stored scope with the stored refresh token"
    )
    assert forms[0]["resource"] == [_URL], "the refresh is bound to the server's resource"
    tokens = {decrypt_token(fernet, r.access_token_encrypted) for r in results}
    assert tokens == {"new-at"}, "every caller gets the winner's token"
    rotated = results[0].refresh_token_encrypted
    assert rotated is not None and decrypt_token(fernet, rotated) == "rt-2", "rotation is stored"
    assert results[0].refresh_started_at is None, "the claim is released after the write"


async def test_a_widened_scope_is_refused_and_nothing_is_stored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    fernet = make_fernet()
    row = await _seed(db_session, fernet, account_id=None, tenant_id=tenant.id)

    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "x", "scope": "read write"})

    with pytest.raises(ConnectorScopeWidenedError):
        await refresh_connector_token(
            db_session_factory,
            tenant_id=tenant.id,
            token_id=row.id,
            fernet=fernet,
            http_factory=_pinned(handler),
        )
    async with db_session_factory() as session:
        kept = await store.get_connector_token_by_id(session, tenant_id=tenant.id, token_id=row.id)
    assert kept is not None and kept.scope == "read", "the granted scope is never widened"
    assert decrypt_token(fernet, kept.access_token_encrypted) == "old-at", "no token is stored"
    assert kept.refresh_started_at is None, "a refused refresh releases its claim"


async def test_invalid_grant_deletes_the_row_so_a_reconnect_shows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    fernet = make_fernet()
    await _seed(db_session, fernet, account_id=account.id, tenant_id=tenant.id)

    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    token = await connector_access_token(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=_AGENT,
        server_url=_URL,
        requester_account_id=account.id,
        hide_personal=False,
        fernet=fernet,
        http_factory=_pinned(handler),
    )
    assert token is None, "a refused grant reads as not connected"
    async with db_session_factory() as session:
        urls = await store.list_connector_token_urls(
            session, tenant_id=tenant.id, agent_id=_AGENT, account_id=account.id
        )
    assert urls == frozenset(), "the dead grant is gone"


async def test_no_personal_token_where_personal_servers_are_hidden(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    fernet = make_fernet()
    await _seed(db_session, fernet, account_id=account.id, tenant_id=tenant.id)

    async def lookup(requester: uuid.UUID, *, hide: bool) -> ConnectorTokenRow | None:
        return await connector_token_for_turn(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=_AGENT,
            server_url=_URL,
            requester_account_id=requester,
            hide_personal=hide,
        )

    mine = await lookup(account.id, hide=False)
    assert mine is not None and mine.account_id == account.id, "the requester gets their own"
    hidden = await lookup(account.id, hide=True)
    assert hidden is None, "hide_personal_for must never yield a personal token"
    other = await make_account(db_session, tenant=tenant)
    theirs = await lookup(other.id, hide=False)
    assert theirs is None, "a token is keyed by its requester, never shared with another"


def test_refresh_owner_is_ma_only_on_the_managed_agents_profile() -> None:
    ma = ResolvedBackend(backend="anthropic", profile=MANAGED_AGENTS.profile_id, model=None)
    other = ma.model_copy(update={"profile": "openai.responses"})
    assert refresh_owner_for(ma) == "ma", "the MA vault refreshes an MA agent's grant"
    assert refresh_owner_for(other) == "daimon", "Daimon refreshes every other backend's grant"


def test_a_grant_without_a_daimon_token_reads_as_ungranted() -> None:
    account, agent = uuid.uuid4(), uuid.uuid4()
    grant = McpOAuthGrantRow(agent_id=agent, account_id=account, mcp_server_url=_URL)

    def hidden(tokens: list[str] | None) -> frozenset[str]:
        return hidden_mcp_server_names(
            [grant],
            agent_id=agent,
            account_id=account,
            shared_server_urls=[],
            server_urls={"notion": _URL},
            connector_token_urls=tokens,
        )

    assert hidden(None) == frozenset(), "MA: the marker is enough"
    assert hidden([]) == {"notion"}, "non-MA: a marker with no Daimon token shows as a reconnect"
    shown = hidden([_URL + "/"])
    assert shown == frozenset(), "a held token satisfies the grant, trailing slash ignored"


# Pinned HTTP: refresh, exchange and discovery refuse redirects, private
# addresses and rebinding.


async def _refresh(http: httpx.AsyncClient) -> object:
    return await refresh_access_token(
        http,
        token_endpoint=_TOKEN_URL,
        refresh_token="rt",
        client=_CLIENT,
        scope=None,
        resource=None,
    )


async def _exchange(http: httpx.AsyncClient) -> object:
    return await exchange_authorization_code(
        http,
        token_endpoint=_TOKEN_URL,
        code="c",
        code_verifier="v" * 64,
        client=_CLIENT,
        redirect_uri="https://daimon.example/cb",
        resource=None,
    )


async def _discover(http: httpx.AsyncClient) -> object:
    return await discover_authorization_server(http, mcp_server_url=_URL)


_CALLS = pytest.mark.parametrize("call", [_refresh, _exchange, _discover], ids=lambda c: c.__name__)


@_CALLS
async def test_redirects_are_never_followed(
    call: Callable[[httpx.AsyncClient], Awaitable[object]],
) -> None:
    seen: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["host"])
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})

    with pytest.raises(Exception):  # noqa: B017 - each call reports a non-200 its own way
        async with _pinned(handler)() as http:
            await call(http)
    assert seen and "169.254.169.254" not in seen, "the redirect target is never requested"


@_CALLS
async def test_private_addresses_are_refused(
    call: Callable[[httpx.AsyncClient], Awaitable[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def private(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    requested: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requested.append(str(req.url))
        return httpx.Response(200, json={})

    client = pinned_http_client(
        resolve=resolve_public_address, transport=httpx.MockTransport(handler)
    )
    with pytest.raises((AddressRefusedError, McpOAuthTokenRefusedError)):
        async with client as http:
            await call(http)
    assert requested == [], "a name resolving to a private address is never connected to"


async def test_one_resolution_per_host_so_rebinding_cannot_move_it() -> None:
    resolved: list[str] = []
    connected: list[tuple[str, str]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        connected.append((req.url.host, req.headers["host"]))
        return httpx.Response(404)

    with pytest.raises(Exception):  # noqa: B017 - nothing is discoverable here
        async with _pinned(handler, resolved)() as http:
            await _discover(http)
    assert resolved.count("mcp.example.com") == 1, "each host is resolved once per client"
    assert len(connected) > 1, "discovery makes several requests to the same host"
    assert {ip for ip, _ in connected} == {_PUBLIC_IP}, "every request goes to the pinned address"
    assert {host for _, host in connected} == {"mcp.example.com"}, "the Host header is kept"


def test_watches_never_read_connector_tokens() -> None:
    root = Path(__file__).resolve().parents[2] / "daimon" / "core"
    readers: list[str] = []
    for path in root.rglob("*.py"):
        if "watch" not in path.name:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""] + [a.name for a in node.names]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any("connector_tokens" in name for name in names):
                readers.append(str(path.relative_to(root)))
    assert readers == [], f"watch code must not read connector tokens: {readers}"
