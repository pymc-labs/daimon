"""A session's own relay vault carries its session-bound credential to the identity relays."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import jwt as pyjwt
from anthropic import AsyncAnthropic
from daimon.core.mcp_identity import (
    RELAY_CLAIM,
    RelayBinding,
    encode_seed,
    generate_signing_key,
    relay_url,
)
from daimon.core.mcp_vault import (
    RELAY_METADATA_KEY,
    archive_relay_vaults,
    chat_mcp_credential,
    create_relay_vault,
    refresh_mounted_mcp_vaults,
)
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PUBLIC_URL = "https://daimon.example.com/mcp"
RELAY = relay_url(PUBLIC_URL, "tools")
OTHER = relay_url(PUBLIC_URL, "Other/")
SCOPE = Scope.legacy_host_authorized(call_site="test")
NOW = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
SECRET = b"s" * 32


class _Vault:
    """One vault's credentials, as the provider would hold them, tokens included."""

    def __init__(self, display_name: str, creds: list[dict[str, Any]] | None = None) -> None:
        self.display_name = display_name
        self.creds = creds or []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.archived = False

    def by_url(self, url: str) -> dict[str, Any]:
        (cred,) = [c for c in self.creds if c["auth"]["mcp_server_url"] == url]
        return cred

    def vault(self) -> dict[str, Any]:
        return {
            "id": "vlt_1",
            "type": "vault",
            "display_name": self.display_name,
            "metadata": {},
            "archived_at": None,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
        }

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path == "/v1/vaults" and req.method == "POST":
                self.display_name = json.loads(req.content)["display_name"]
                return httpx.Response(200, json=self.vault())
            if path == "/v1/vaults/vlt_1/archive" and req.method == "POST":
                self.archived = True
                return httpx.Response(200, json=self.vault())
            if path == "/v1/vaults" and req.method == "GET":
                vault: dict[str, Any] = {
                    "id": "vlt_1",
                    "type": "vault",
                    "display_name": self.display_name,
                    "metadata": {},
                    "archived_at": None,
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                }
                return httpx.Response(200, json={"data": [vault], "has_more": False})
            if path == "/v1/vaults/vlt_1/credentials" and req.method == "GET":
                public = [
                    {**c, "auth": {k: v for k, v in c["auth"].items() if k != "token"}}
                    for c in self.creds
                ]
                return httpx.Response(200, json={"data": public, "has_more": False})
            if path == "/v1/vaults/vlt_1" and req.method == "GET":
                return httpx.Response(200, json=self.vault())
            body = json.loads(req.content)
            if path == "/v1/vaults/vlt_1/credentials" and req.method == "POST":
                cred: dict[str, Any] = {
                    "id": f"vcrd_{len(self.creds)}",
                    "type": "vault_credential",
                    "vault_id": "vlt_1",
                    "metadata": body.get("metadata") or {},
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-01T00:00:00Z",
                    "auth": body["auth"],
                }
                self.creds.append(cred)
                self.writes.append(("create", body))
                return httpx.Response(200, json=cred)
            if path.startswith("/v1/vaults/vlt_1/credentials/") and req.method == "POST":
                (cred,) = [c for c in self.creds if c["id"] == path.rsplit("/", 1)[-1]]
                cred["metadata"] = body.get("metadata") or {}
                cred["auth"] = {**cred["auth"], **body["auth"]}
                self.writes.append(("update", body))
                return httpx.Response(200, json=cred)
            raise AssertionError(f"unexpected call: {req.method} {req.url}")

        return handle


def _client(vault: _Vault) -> AsyncAnthropic:
    transport = httpx.MockTransport(vault.handler())
    return AsyncAnthropic(api_key="sk-test", http_client=httpx.AsyncClient(transport=transport))


async def _account(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord")
        account = await make_account(db, tenant=tenant)
    return account.id


def _signed(cred: dict[str, Any]) -> dict[str, Any]:
    return pyjwt.decode(
        cred["auth"]["token"], SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )


async def _refresh(
    factory: async_sessionmaker[AsyncSession],
    vault: _Vault,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    relay_urls: tuple[str, ...],
    now: dt.datetime,
) -> None:
    await refresh_mounted_mcp_vaults(
        _client(vault), vault_ids=["vlt_1"], account_id=account_id, agent_id=agent_id,
        jwt_secret=SECRET, public_url=PUBLIC_URL, now=now, session_factory=factory,
        scope=SCOPE, relay_urls=relay_urls,
    )  # fmt: skip


async def _relay_vault(
    factory: async_sessionmaker[AsyncSession], binding: RelayBinding, *urls: str
) -> tuple[_Vault, uuid.UUID, uuid.UUID]:
    account_id, agent_id = await _account(factory), uuid.uuid4()
    vault = _Vault("unused")
    await create_relay_vault(
        _client(vault), session_factory=factory, account_id=account_id, agent_id=agent_id,
        jwt_secret=SECRET, relay_urls=urls, relay=binding, now=NOW, scope=SCOPE,
    )  # fmt: skip
    return vault, account_id, agent_id


async def test_relay_vault_holds_one_session_bound_credential_per_relay_url(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = uuid.uuid4()
    seed = encode_seed(generate_signing_key())
    vault, account_id, agent_id = await _relay_vault(
        db_session_factory, RelayBinding("Helper", key), RELAY, OTHER
    )
    assert vault.display_name == f"daimon-relay:{account_id}:{agent_id}", "its own vault"
    first, second = vault.by_url(RELAY), vault.by_url(OTHER)
    assert first["auth"]["token"] == second["auth"]["token"], "one credential serves each relay"
    assert first["metadata"][RELAY_METADATA_KEY] == "true", "the credential is marked"
    assert _signed(first)[RELAY_CLAIM] == {"agent_name": "Helper", "session": str(key)}, (
        "the session's binding is signed into the credential"
    )
    assert pyjwt.get_unverified_header(first["auth"]["token"])["alg"] == "HS256", (
        "the runtime only ever holds Daimon's own chat token, never an identity token"
    )
    assert seed not in json.dumps(vault.creds), "no signing key reaches the vault"


async def test_headless_relay_credential_names_no_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vault, _, _ = await _relay_vault(db_session_factory, RelayBinding("Helper"), RELAY)
    assert _signed(vault.by_url(RELAY))[RELAY_CLAIM] == {"agent_name": "Helper"}, (
        "a routine's credential binds no session, so the relay names no person for it"
    )


async def test_refresh_keeps_the_binding_and_adds_a_relay_attached_since(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = uuid.uuid4()
    vault, account_id, agent_id = await _relay_vault(
        db_session_factory, RelayBinding("Helper", key), RELAY
    )
    old = vault.by_url(RELAY)["auth"]["token"]

    await _refresh(
        db_session_factory, vault, account_id, agent_id, relay_urls=(RELAY, OTHER),
        now=NOW + dt.timedelta(minutes=20),
    )  # fmt: skip

    renewed, added = vault.by_url(RELAY), vault.by_url(OTHER)
    assert renewed["auth"]["token"] != old, "a credential near expiry is re-minted"
    assert added["auth"]["token"] == renewed["auth"]["token"], "the new relay gets the same one"
    assert _signed(added)[RELAY_CLAIM]["session"] == str(key), "the binding survives a refresh"


async def test_fresh_relay_vault_and_other_vaults_are_left_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vault, account_id, agent_id = await _relay_vault(
        db_session_factory, RelayBinding("Helper", uuid.uuid4()), RELAY
    )
    writes = len(vault.writes)
    for urls in ((RELAY,), (RELAY + "/",)):
        await _refresh(db_session_factory, vault, account_id, agent_id, relay_urls=urls, now=NOW)
    assert len(vault.writes) == writes, "a fresh relay vault holding each server is not rewritten"

    async with db_session_factory() as db, db.begin():
        payload = await chat_mcp_credential(
            db, account_id=account_id, agent_id=agent_id, jwt_secret=SECRET,
            public_url=PUBLIC_URL, now=NOW,
        )  # fmt: skip
    shared = _Vault("shared", [{**payload, "id": "vcrd_main", "type": "vault_credential",
        "vault_id": "vlt_1", "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z"}])  # fmt: skip
    await _refresh(db_session_factory, shared, account_id, agent_id, relay_urls=(RELAY,), now=NOW)
    assert [cred["auth"]["mcp_server_url"] for cred in shared.creds] == [PUBLIC_URL], (
        "the caller's shared vault never gets a relay credential"
    )


async def test_closing_a_session_archives_only_its_relay_vault() -> None:
    relay, shared = _Vault(f"daimon-relay:{uuid.uuid4()}:{uuid.uuid4()}"), _Vault("daimon-mcp:x")
    for vault in (relay, shared):
        await archive_relay_vaults(_client(vault), ["vlt_1"], scope=SCOPE)
    assert (relay.archived, shared.archived) == (True, False), "the shared vault stays"
