"""The grant write and the shared-token mirror racing for one vault URL.

MA keeps one credential per server URL in a vault: a second create at the URL
is a 409, and an update or delete of an id that is gone is a 404.
`put_mcp_oauth_credential` (the per-turn grant mirror) and
`mirror_credentials_into_vault` (the shared-token mirror) both write that
slot. Their callers hold the per-(account, agent) vault lock; the unit tests
below call them bare, as a writer outside the lock would, so each must still
survive the other landing between its list and its write. Found by the
VaultSlot TLA+ model (`formal/oauth`): configs GrantVsMirror and
GrantVsStaleMirror. The real-Postgres tests run racing turns through the
locked paths, which the bounded retry alone did not survive.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core import agent_mcp_credentials, sessions
from daimon.core.agent_mcp_credentials import (
    METADATA_VERSION_KEY,
    ResolvedMcpCredential,
    mirror_credentials_into_vault,
    resolve_agent_mcp_credentials,
    resync_mounted_shared_vault,
    save_agent_mcp_credential,
    sync_agent_mcp_credentials,
    sync_shared_mcp_credentials,
)
from daimon.core.config import McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth.complete import complete_mcp_oauth_flow
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential
from daimon.core.mcp_vault import shared_vault_name
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_mcp_credentials as credential_store
from daimon.core.stores import connector_tokens as connector_tokens_store
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import ConnectorTokenRow, McpOAuthFlowRow, Role
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.crypto import make_fernet
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.ids import Scope
from mux.testing.drivers.anthropic.ma import list_response, session_response
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

_NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
_URL = "https://mcp.notion.com/mcp"
_VAULT = "vlt_1"

Hook = Callable[[], Awaitable[None]]


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )


class FakeVault:
    """A stateful MA vault that enforces one credential per URL.

    `after_delete` runs once, right after the first successful delete, and
    `before_update` once, right before the first update is applied: each is
    where a concurrent writer lands in the traces this file pins.
    """

    def __init__(self, creds: list[dict[str, Any]]) -> None:
        self.creds: dict[str, dict[str, Any]] = {c["id"]: c for c in creds}
        self._next = 0
        self.writes: list[dict[str, Any]] = []
        self.after_delete: Hook | None = None
        self.before_update: Hook | None = None

    def _holder(self, url: str) -> dict[str, Any] | None:
        for cred in self.creds.values():
            if cred["auth"]["mcp_server_url"].rstrip("/") == url.rstrip("/"):
                return cred
        return None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base = f"/v1/vaults/{_VAULT}/credentials"
        if request.method == "GET" and path == base:
            data = list(self.creds.values())
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "has_more": False,
                    "first_id": data[0]["id"] if data else None,
                    "last_id": data[-1]["id"] if data else None,
                },
            )
        if request.method == "POST" and path == base:
            body = json.loads(request.content)
            self.writes.append(body)
            auth = body["auth"]
            if self._holder(auth["mcp_server_url"]) is not None:
                return _error(409, "A credential already exists for this MCP server URL.")
            self._next += 1
            cred_id = f"vcrd_{self._next}"
            cred: dict[str, Any] = {
                "id": cred_id,
                "type": "vault_credential",
                "vault_id": _VAULT,
                "auth": {"type": auth["type"], "mcp_server_url": auth["mcp_server_url"]},
                "metadata": body.get("metadata"),
                "display_name": body.get("display_name"),
                "created_at": "2026-09-24T12:00:00Z",
                "updated_at": "2026-09-24T12:00:00Z",
                "archived_at": None,
            }
            self.creds[cred_id] = cred
            return httpx.Response(200, json=cred)
        if path.startswith(base + "/"):
            cred_id = path.rsplit("/", 1)[-1]
            if request.method == "POST":
                if self.before_update is not None:
                    hook, self.before_update = self.before_update, None
                    await hook()
                if cred_id not in self.creds:
                    return _error(404, "credential not found")
                body = json.loads(request.content)
                self.writes.append(body)
                self.creds[cred_id]["metadata"] = body.get("metadata")
                return httpx.Response(200, json=self.creds[cred_id])
            if request.method == "DELETE":
                if self.creds.pop(cred_id, None) is None:
                    return _error(404, "credential not found")
                if self.after_delete is not None:
                    hook, self.after_delete = self.after_delete, None
                    await hook()
                return httpx.Response(200, json={"id": cred_id, "type": "vault_credential_deleted"})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    def client(self) -> AsyncAnthropic:
        transport = httpx.MockTransport(self.handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
        return AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)


def _static(cred_id: str, version: str) -> dict[str, Any]:
    return {
        "id": cred_id,
        "type": "vault_credential",
        "vault_id": _VAULT,
        "auth": {"type": "static_bearer", "mcp_server_url": _URL},
        "metadata": {METADATA_VERSION_KEY: version},
        "display_name": None,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "archived_at": None,
    }


def _shared(version: str) -> tuple[ResolvedMcpCredential, ...]:
    return (ResolvedMcpCredential(mcp_server_url=_URL, token="agent-tok", version=version),)


async def _put_grant(client: AsyncAnthropic) -> str:
    return await put_mcp_oauth_credential(
        client,
        vault_id=_VAULT,
        mcp_server_url=_URL,
        tokens=TokenResponse(access_token="acc", refresh_token="ref", expires_in=3600),
        client=ClientRegistration(client_id="cid"),
        token_endpoint="https://mcp.notion.com/token",
        resource=None,
        now=_NOW,
    )


async def test_a_sign_in_survives_a_mirror_recreating_the_shared_token_mid_write() -> None:
    """Trace: grant write lists the shared token, deletes it; a turn's mirror
    lists the empty slot and recreates the shared token; the grant's create
    must not end in a 409 that loses a sign-in whose flow is already spent."""
    vault = FakeVault([_static("vcrd_shared", "v1")])
    client = vault.client()

    async def concurrent_mirror() -> None:
        await mirror_credentials_into_vault(client, vault_id=_VAULT, credentials=_shared("v1"))

    vault.after_delete = concurrent_mirror

    credential_id = await _put_grant(client)

    holder = vault._holder(_URL)  # pyright: ignore[reportPrivateUsage]
    assert holder is not None and holder["auth"]["type"] == "mcp_oauth", (
        "the person's grant must hold the URL once the sign-in finishes"
    )
    assert holder["id"] == credential_id


async def test_a_mirror_update_survives_the_grant_replacing_the_shared_token() -> None:
    """Trace: a turn's mirror lists a stale shared token; the grant write deletes
    it and stores the grant; the mirror's in-place update then finds the id
    gone. The turn must not fail on that 404, and the grant must stay."""
    vault = FakeVault([_static("vcrd_shared", "v1")])
    client = vault.client()

    async def concurrent_grant() -> None:
        await _put_grant(client)

    vault.before_update = concurrent_grant

    await mirror_credentials_into_vault(client, vault_id=_VAULT, credentials=_shared("v2"))

    holder = vault._holder(_URL)  # pyright: ignore[reportPrivateUsage]
    assert holder is not None and holder["auth"]["type"] == "mcp_oauth", (
        "the mirror must leave the person's grant alone after re-reading"
    )


def _grant_row(fernet: MultiFernet, *, version: dt.datetime) -> ConnectorTokenRow:
    return ConnectorTokenRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        server_url=_URL,
        access_token_encrypted=encrypt_token(fernet, "fresh-at"),
        refresh_token_encrypted=encrypt_token(fernet, "rt"),
        token_endpoint="https://mcp.notion.com/token",
        client_id="cid",
        client_secret_encrypted=None,
        token_endpoint_auth_method="none",
        scope=None,
        resource=None,
        expires_at=version + dt.timedelta(hours=1),
        refresh_owner="daimon",
        refresh_started_at=None,
        created_at=version,
        updated_at=version,
    )


def _held(kind: str, stamp: str | None) -> dict[str, Any]:
    cred = _static("vcrd_held", "x")
    cred["auth"]["type"] = kind
    cred["metadata"] = None if stamp is None else {METADATA_VERSION_KEY: stamp}
    return cred


@pytest.mark.parametrize(
    ("held", "outcome"),
    [
        (None, "created"),
        ("current", "kept"),
        ("older", "updated"),
        ("anthropic_grant", "replaced"),
        ("agent_token", "replaced"),
        ("pasted_token", "kept"),
    ],
)
async def test_a_grant_mirror_writes_the_slot_by_what_holds_it(
    held: str | None, outcome: str
) -> None:
    """The per-turn grant mirror's table (`mirror_grants_into_vault`): a sign-in
    outranks the agent's shared token and an Anthropic-refreshed grant, keeps a
    person's own pasted token, and updates its own copy in place."""
    fernet = make_fernet()
    grant = _grant_row(fernet, version=_NOW)
    current = grant.updated_at.isoformat()
    existing = {
        "current": _held("mcp_oauth", current),
        "older": _held("mcp_oauth", "2026-01-01T00:00:00+00:00"),
        "anthropic_grant": _held("mcp_oauth", None),
        "agent_token": _held("static_bearer", "v1"),
        "pasted_token": _held("static_bearer", None),
    }.get(held or "")
    vault = FakeVault([existing] if existing is not None else [])
    await agent_mcp_credentials.mirror_grants_into_vault(
        vault.client(),
        vault_id=_VAULT,
        grants=(grant,),
        fernet=fernet,
        scope=Scope.legacy_host_authorized(call_site="test"),
    )
    holder = vault._holder(_URL)  # pyright: ignore[reportPrivateUsage]
    assert holder is not None, "the URL keeps a credential"
    if outcome == "kept":
        assert vault.writes == [] and holder["id"] == "vcrd_held", f"{held} is left alone"
        return
    assert holder["metadata"] == {METADATA_VERSION_KEY: current}, "stamped with this grant"
    (write,) = vault.writes
    assert write["auth"]["access_token"] == "fresh-at", "the fresh access token is copied"
    assert "refresh" not in write["auth"], "Anthropic never gets the refresh token"
    if outcome == "updated":
        assert holder["id"] == "vcrd_held", "an own copy is updated in place, never deleted"
        assert "mcp_server_url" not in write["auth"], "the URL of a credential is immutable"
    else:
        assert holder["auth"]["type"] == "mcp_oauth" and holder["id"] != "vcrd_held", (
            f"{held} gives way to the person's grant"
        )


# ----- Three turns mirroring a new sign-in into one vault (real Postgres) -----

_PUBLIC_URL = "https://daimon.example/mcp"
# How long MA takes between the grant's delete and its create: the window a
# concurrent turn's mirror lands in. Generous, so a mirror that is not locked
# out always finishes inside it.
_DELETE_WINDOW_S = 0.3


class SlowVault(FakeVault):
    """`FakeVault` plus the vault and agent listings the full callback and a
    turn's remirror read, and a slow delete that opens the window."""

    def __init__(self, creds: list[dict[str, Any]], *, display_name: str) -> None:
        super().__init__(creds)
        self.display_name = display_name
        self.deletes: list[asyncio.Event] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/vaults":
            return list_response(
                [
                    {
                        "id": _VAULT,
                        "type": "vault",
                        "display_name": self.display_name,
                        "metadata": None,
                        "archived_at": None,
                        "created_at": "2026-09-01T00:00:00Z",
                    }
                ]
            )
        if request.method == "GET" and path == "/v1/agents":
            return list_response([])
        if request.method == "POST" and path == "/v1/sessions":
            return session_response(session_id=f"sesn_{uuid.uuid4().hex[:8]}")
        response = await super().handler(request)
        if request.method == "DELETE" and response.status_code == 200:
            deleted = asyncio.Event()
            deleted.set()
            self.deletes.append(deleted)
            await asyncio.sleep(_DELETE_WINDOW_S)
        return response


async def _seed_flow_and_shared_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    *,
    url: str,
    shared: bool = True,
) -> McpOAuthFlowRow:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_oauth")
        request = await requests_store.create_credential_request(
            session,
            token=mint_request_token(),
            kind="mcp_oauth",
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account.id,
            target="notion",
            mcp_server_url=url,
            requester_platform_user_id="requester-1",
            channel_id="chan-1",
            expires_at=_NOW + dt.timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_oauth",
            target_name="daimon",
            requested_work=None,
        )
        flow = await flows_store.create_flow(
            session,
            state="st_" + uuid.uuid4().hex,
            request_token=request.token,
            tenant_id=tenant.id,
            account_id=account.id,
            agent_id=agent_id,
            server_name="notion",
            mcp_server_url=url,
            redirect_uri="https://daimon.example/oauth/mcp/callback",
            code_verifier="v" * 64,
            expires_at=_NOW + dt.timedelta(minutes=10),
        )
        saved = await flows_store.save_flow_client(
            session,
            state=flow.state,
            client_id="cid",
            client_secret_encrypted=None,
            token_endpoint_auth_method="none",
            token_endpoint="https://mcp.notion.com/token",
            authorization_endpoint="https://mcp.notion.com/authorize",
            resource="https://mcp.notion.com",
            scope="default",
        )
        assert saved is not None
    if not shared:
        return saved
    # The agent's shared token for the same server: what every turn mirrors.
    await save_agent_mcp_credential(
        sessionmaker=sessionmaker,
        fernet=fernet,
        tenant_id=saved.tenant_id,
        agent_id=saved.agent_id,
        mcp_server_url=url,
        plaintext_token="agent-tok",
    )
    return saved


# How each racing turn reaches the vault, and whether the agent also holds a
# shared token at the URL (an OAuth-only agent skips the shared-token read):
#   remirror       - a reused session's `sync_agent_mcp_credentials`.
#   fresh_session  - a new session's `create_session`.
_RACER_PATHS = (("remirror", True), ("fresh_session", True), ("remirror", False))


@pytest.mark.parametrize(("path", "shared"), _RACER_PATHS)
async def test_three_turns_mirroring_a_new_sign_in_leave_the_grant_in_the_vault(
    db_clean: None, db_schema: str, monkeypatch: pytest.MonkeyPatch, path: str, shared: bool
) -> None:
    """The sign-in stores Daimon's grant; the next three turns for the same
    (account, agent) each mirror it over the agent's shared token at the URL.
    Each replace opens a delete→create window; without the per-(account,
    agent) vault lock the turns collide in it and end in a 409.

    Separate engines, committed rows: the advisory lock is connection-scoped,
    so the shared single-connection fixture would hide it.
    """
    url = _URL
    dsn = os.environ["DAIMON_DATABASE__TEST_URL"]
    engines = [build_test_engine(dsn, db_schema, poolclass=NullPool) for _ in range(4)]
    factories = [async_sessionmaker(bind=e, expire_on_commit=False) for e in engines]
    fernet = make_fernet()
    try:
        flow = await _seed_flow_and_shared_token(factories[0], fernet, url=url, shared=shared)
        display = f"daimon-mcp:{flow.account_id}:{flow.agent_id}"
        jwt_cred = _static("vcrd_jwt", "v0")
        jwt_cred["auth"] = {"type": "static_bearer", "mcp_server_url": _PUBLIC_URL}
        held = [jwt_cred]
        for stored in await resolve_agent_mcp_credentials(
            sessionmaker=factories[0],
            fernet=fernet,
            tenant_id=flow.tenant_id,
            agent_id=flow.agent_id,
        ):
            held.append(_static("vcrd_shared", stored.version))
        vault = SlowVault(held, display_name=display)
        client = vault.client()

        def token_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            )

        completion = await complete_mcp_oauth_flow(
            httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
            client,
            flow=flow,
            code="code123",
            fernet=fernet,
            jwt_secret=b"x" * 32,
            public_url=_PUBLIC_URL,
            now=_NOW,
            session_factory=factories[0],
            default=DeploymentDefault(agent_name="daimon", environment_name="default"),
        )
        assert vault.writes == [], "the sign-in itself writes no vault"

        async def memory_mount(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"type": "memory_store", "memory_store_id": "memstore_test"}

        monkeypatch.setattr(sessions, "ensure_memory_store_and_mount", memory_mount)

        async def turn(nth: int) -> None:
            if path == "remirror":
                await sync_agent_mcp_credentials(
                    client,
                    sessionmaker=factories[nth],
                    fernet=fernet,
                    tenant_id=flow.tenant_id,
                    agent_id=flow.agent_id,
                    account_id=flow.account_id,
                    jwt_secret=b"x" * 32,
                    public_url=_PUBLIC_URL,
                    now=_NOW,
                )
                return
            await sessions.create_session(
                client,
                agent=ma_agent(id="ag_oauth"),
                environment=ma_environment(),
                mcp_settings=McpSettings(
                    jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(_PUBLIC_URL)
                ),
                account_id=flow.account_id,
                tenant_id=flow.tenant_id,
                agent_uuid=flow.agent_id,
                session_factory=factories[nth],
                fernet=fernet,
                tool_safety=OPEN_TOOL_SAFETY,
            )

        results = await asyncio.gather(turn(1), turn(2), turn(3), return_exceptions=True)
        async with factories[0]() as session:
            row = await connector_tokens_store.get_connector_token_by_id(
                session, tenant_id=flow.tenant_id, token_id=completion.connector_token_id
            )
    finally:
        for engine in engines:
            await engine.dispose()

    assert [r for r in results if isinstance(r, BaseException)] == [], (
        "no turn may fail on the grant replacing the agent's credential"
    )
    holder = vault._holder(url)  # pyright: ignore[reportPrivateUsage]
    assert holder is not None and holder["auth"]["type"] == "mcp_oauth", (
        "the person's grant must hold the URL after the next turn"
    )
    assert row is not None and holder["metadata"] == {
        METADATA_VERSION_KEY: row.updated_at.isoformat()
    }, "the copy is stamped with the grant it carries"
    grants = [w for w in vault.writes if w["auth"]["type"] == "mcp_oauth"]
    assert len(grants) == 1, "one turn copies the grant; the others find it current"
    assert grants[0]["auth"]["access_token"] == "at" and "refresh" not in grants[0]["auth"], (
        "the copy carries the access token only: Daimon, not Anthropic, refreshes"
    )


# ----- A detach while a mirror waits for the vault lock (real Postgres) -----

# Every writer that copies the agent's stored tokens into a vault:
#   remirror       - a reused session's `sync_agent_mcp_credentials`.
#   fresh_session  - a new session's `create_session`.
#   shared_create  - a new shared thread's `sync_shared_mcp_credentials`.
#   shared_resync  - a reused shared thread's `resync_mounted_shared_vault`.
_MIRROR_PATHS = ("remirror", "fresh_session", "shared_create", "shared_resync")


@pytest.mark.parametrize("path", _MIRROR_PATHS)
async def test_a_token_removed_while_a_mirror_waits_for_the_lock_is_not_written_back(
    db_clean: None, db_schema: str, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Trace: a mirror starts while the agent holds the token; the detach then
    deletes the stored token and clears the vault copy under the vault lock
    (`forget_mirrored_mcp_token`); the mirror takes the lock last.

    A mirror that read the tokens before the lock writes the removed token back,
    and it keeps authenticating. Each path must read under the lock.
    """
    dsn = os.environ["DAIMON_DATABASE__TEST_URL"]
    engine = build_test_engine(dsn, db_schema, poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    fernet = make_fernet()
    owner = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            tenant = await make_tenant(session)
            account = await make_account(session, tenant=tenant)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_detach")
        await save_agent_mcp_credential(
            sessionmaker=factory,
            fernet=fernet,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url=_URL,
            plaintext_token="agent-tok",
        )
        (stored,) = await resolve_agent_mcp_credentials(
            sessionmaker=factory, fernet=fernet, tenant_id=tenant.id, agent_id=agent_id
        )
        # The copy an earlier turn mirrored, current with the stored token.
        copy = _static("vcrd_copy", stored.version)
        vault = SlowVault([copy], display_name=f"daimon-mcp:{account.id}:{agent_id}")
        client = vault.client()

        at_lock = asyncio.Event()
        detached = asyncio.Event()
        racer: list[asyncio.Task[Any] | None] = []
        real_lock = agent_mcp_credentials.hold_agent_vault_lock

        @asynccontextmanager
        async def lock_after_the_detach(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            if asyncio.current_task() in racer:
                at_lock.set()
                await detached.wait()
            async with real_lock(*args, **kwargs) as locked:
                yield locked

        async def vault_id(*_args: Any, **_kwargs: Any) -> str:
            return _VAULT

        async def memory_mount(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"type": "memory_store", "memory_store_id": "memstore_test"}

        async def mounted_vault(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(display_name=shared_vault_name(owner, agent_id))

        for module in (agent_mcp_credentials, sessions):
            monkeypatch.setattr(
                module, "hold_agent_vault_lock", lock_after_the_detach, raising=False
            )
            monkeypatch.setattr(module, "ensure_agent_mcp_vault", vault_id, raising=False)
        monkeypatch.setattr(agent_mcp_credentials, "ensure_shared_mcp_vault", vault_id)
        monkeypatch.setattr(agent_mcp_credentials, "retrieve_vault", mounted_vault)
        monkeypatch.setattr(sessions, "ensure_memory_store_and_mount", memory_mount)

        async def mirror() -> None:
            racer.append(asyncio.current_task())
            if path == "remirror":
                await sync_agent_mcp_credentials(
                    client,
                    sessionmaker=factory,
                    fernet=fernet,
                    tenant_id=tenant.id,
                    agent_id=agent_id,
                    account_id=account.id,
                    jwt_secret=b"x" * 32,
                    public_url=_PUBLIC_URL,
                    now=_NOW,
                )
            elif path == "fresh_session":
                await sessions.create_session(
                    client,
                    agent=ma_agent(id="ag_detach"),
                    environment=ma_environment(),
                    mcp_settings=McpSettings(
                        jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(_PUBLIC_URL)
                    ),
                    account_id=account.id,
                    tenant_id=tenant.id,
                    agent_uuid=agent_id,
                    session_factory=factory,
                    fernet=fernet,
                    tool_safety=OPEN_TOOL_SAFETY,
                )
            elif path == "shared_create":
                await sync_shared_mcp_credentials(
                    client,
                    sessionmaker=factory,
                    fernet=fernet,
                    tenant_id=tenant.id,
                    agent_id=agent_id,
                    owner=owner,
                )
            else:
                await resync_mounted_shared_vault(
                    client,
                    sessionmaker=factory,
                    fernet=fernet,
                    tenant_id=tenant.id,
                    agent_id=agent_id,
                    owner=owner,
                    mounted_vault_ids=(_VAULT,),
                )

        async def detach() -> None:
            await at_lock.wait()
            async with factory() as session, session.begin():
                assert await credential_store.delete_credential(
                    session, tenant_id=tenant.id, agent_id=agent_id, mcp_server_url=_URL
                )
            vault.creds.pop("vcrd_copy")  # `forget_mirrored_mcp_token`, under the lock
            detached.set()

        await asyncio.wait_for(asyncio.gather(mirror(), detach()), timeout=10)
    finally:
        await engine.dispose()

    assert vault._holder(_URL) is None, (  # pyright: ignore[reportPrivateUsage]
        "a token removed from the agent must not come back into the vault"
    )
