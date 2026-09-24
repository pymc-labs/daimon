"""The grant write and the shared-token mirror racing for one vault URL.

MA keeps one credential per server URL in a vault: a second create at the URL
is a 409, and an update or delete of an id that is gone is a 404.
`put_mcp_oauth_credential` (after the callback has spent the flow) and
`mirror_credentials_into_vault` (every session create and remirror) both
write that slot. Their callers hold the per-(account, agent) vault lock; the
unit tests below call them bare, as a writer outside the lock would, so each
must still survive the other landing between its list and its write. Found
by the VaultSlot TLA+ model (`formal/oauth`): configs GrantVsMirror and
GrantVsStaleMirror. The last test runs the real callback against three
racing turns on real Postgres, which the bounded retry alone did not
survive.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from collections.abc import Awaitable, Callable
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
    save_agent_mcp_credential,
    sync_agent_mcp_credentials,
)
from daimon.core.config import McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth.complete import McpOAuthCompletion, complete_mcp_oauth_flow
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential
from daimon.core.mcp_vault import GITHUB_COPILOT_MCP_URL
from daimon.core.mcp_vault import ensure_agent_mcp_vault as real_ensure_agent_mcp_vault
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import McpOAuthFlowRow
from daimon.testing.crypto import make_fernet
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import list_response, session_response
from daimon.testing.ma_models import ma_agent, ma_environment
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


# ----- Three turns mirroring while the callback writes the grant (real Postgres) -----

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
    sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet, *, url: str
) -> McpOAuthFlowRow:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
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
    if url == GITHUB_COPILOT_MCP_URL:
        # The agent's GitHub PAT is what every fresh session writes there.
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


# How each racing turn reaches the vault, and the URL the person signs in to:
#   remirror       - a reused session's `sync_agent_mcp_credentials` mirrors the
#                    agent's shared token for the server the grant replaces.
#   fresh_session  - a new session's `create_session` mirrors that same token.
#   copilot        - a new session's `create_session` writes the agent's GitHub
#                    PAT while the person signs in to the GitHub Copilot server.
_RACER_PATHS = ("remirror", "fresh_session", "copilot")


@pytest.mark.parametrize("path", _RACER_PATHS)
async def test_a_sign_in_survives_three_turns_mirroring_while_the_grant_is_written(
    db_clean: None, db_schema: str, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Three turns for the same (account, agent) write the URL the OAuth
    callback is replacing with the person's grant. Each turn lands in the
    grant's delete→create window; without the per-(account, agent) vault lock
    each recreates the agent's credential there, the grant's create is a 409
    every time, and the sign-in (flow already spent) is lost.

    Each turn has already been through `ensure_agent_mcp_vault` when it
    reaches its write, so the grant and that write must both hold the lock:
    either one alone leaves the window open. Every racer takes the same
    path, so each lock the paths hold (the remirror's, `create_session`'s
    mirror, `create_session`'s Copilot write) is the only thing between one
    parametrization and the 409.

    Separate engines, committed rows: the advisory lock is connection-scoped,
    so the shared single-connection fixture would hide it.
    """
    url = GITHUB_COPILOT_MCP_URL if path == "copilot" else _URL
    dsn = os.environ["DAIMON_DATABASE__TEST_URL"]
    engines = [build_test_engine(dsn, db_schema, poolclass=NullPool) for _ in range(4)]
    factories = [async_sessionmaker(bind=e, expire_on_commit=False) for e in engines]
    fernet = make_fernet()
    try:
        flow = await _seed_flow_and_shared_token(factories[0], fernet, url=url)
        display = f"daimon-mcp:{flow.account_id}:{flow.agent_id}"
        jwt_cred = _static("vcrd_jwt", "v0")
        jwt_cred["auth"] = {"type": "static_bearer", "mcp_server_url": _PUBLIC_URL}
        agent_cred = _static("vcrd_shared", "v0")
        if path == "copilot":
            agent_cred["auth"] = {"type": "static_bearer", "mcp_server_url": url}
        else:
            (stored,) = await resolve_agent_mcp_credentials(
                sessionmaker=factories[0],
                fernet=fernet,
                tenant_id=flow.tenant_id,
                agent_id=flow.agent_id,
            )
            agent_cred = _static("vcrd_shared", stored.version)
        vault = SlowVault([jwt_cred, agent_cred], display_name=display)
        client = vault.client()
        grant_done = asyncio.Event()

        def token_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            )

        ensured: list[int] = []

        async def grant() -> McpOAuthCompletion:
            # The callback arrives once every turn holds its vault id.
            while len(ensured) < 3:
                await asyncio.sleep(0.005)
            try:
                return await complete_mcp_oauth_flow(
                    httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
                    client,
                    flow=flow,
                    code="code123",
                    fernet=fernet,
                    jwt_secret=b"x" * 32,
                    public_url=_PUBLIC_URL,
                    now=_NOW,
                    session_factory=factories[0],
                )
            finally:
                grant_done.set()

        gates: dict[asyncio.Task[Any] | None, int] = {}

        async def ensure_then_wait(*args: Any, **kwargs: Any) -> str:
            # The turn has its vault id (the ensure's own lock is released) and
            # reaches its write during the grant's n-th delete→create window,
            # or once the grant is done if there is no n-th window.
            vault_id = await real_ensure_agent_mcp_vault(*args, **kwargs)
            nth = gates[asyncio.current_task()]
            ensured.append(nth)
            while len(vault.deletes) < nth and not grant_done.is_set():
                await asyncio.sleep(0.005)
            return vault_id

        async def agent_pat(**_kwargs: Any) -> str | None:
            return "ghp_agent" if path == "copilot" else None

        async def memory_mount(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"type": "memory_store", "memory_store_id": "memstore_test"}

        monkeypatch.setattr(agent_mcp_credentials, "ensure_agent_mcp_vault", ensure_then_wait)
        monkeypatch.setattr(sessions, "ensure_agent_mcp_vault", ensure_then_wait)
        monkeypatch.setattr(sessions, "get_pat", agent_pat)
        monkeypatch.setattr(sessions, "ensure_memory_store_and_mount", memory_mount)

        async def turn(nth: int) -> None:
            gates[asyncio.current_task()] = nth
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
            )

        grant_result, *turn_results = await asyncio.gather(
            grant(), turn(1), turn(2), turn(3), return_exceptions=True
        )
    finally:
        for engine in engines:
            await engine.dispose()

    assert not isinstance(grant_result, BaseException), (
        f"the sign-in must be stored however many turns write meanwhile; got {grant_result!r}"
    )
    assert [r for r in turn_results if isinstance(r, BaseException)] == [], (
        "no turn may fail on the grant replacing the agent's credential"
    )
    holder = vault._holder(url)  # pyright: ignore[reportPrivateUsage]
    assert holder is not None and holder["auth"]["type"] == "mcp_oauth", (
        "the person's grant must hold the URL once the sign-in finishes"
    )
