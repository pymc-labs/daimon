"""The caller vault carries Daimon's chat credential to an agent's identity relay URLs."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import jwt as pyjwt
from anthropic import AsyncAnthropic
from daimon.core.mcp_identity import encode_seed, generate_signing_key, relay_url
from daimon.core.mcp_vault import (
    RELAY_METADATA_KEY,
    chat_mcp_credential,
    ensure_agent_mcp_vault,
    refresh_agent_mcp_vault,
)
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PUBLIC_URL = "https://daimon.example.com/mcp"
RELAY = relay_url(PUBLIC_URL, "tools")
NOW = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
SECRET = b"s" * 32


class _Vault:
    """One vault's credentials, as the provider would hold them, tokens included."""

    def __init__(self, display_name: str, creds: list[dict[str, Any]] | None = None) -> None:
        self.display_name = display_name
        self.creds = creds or []
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def by_url(self, url: str) -> dict[str, Any]:
        (cred,) = [c for c in self.creds if c["auth"]["mcp_server_url"] == url]
        return cred

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(req: httpx.Request) -> httpx.Response:
            path = req.url.path
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


async def _chat_cred(
    factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID, agent_id: uuid.UUID, url: str
) -> dict[str, Any]:
    async with factory() as db, db.begin():
        payload = await chat_mcp_credential(
            db, account_id=account_id, agent_id=agent_id, jwt_secret=SECRET,
            public_url=url, now=NOW,
        )  # fmt: skip
    return {
        "id": f"vcrd_{uuid.uuid4().hex[:6]}",
        "type": "vault_credential",
        "vault_id": "vlt_1",
        "metadata": payload["metadata"],
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "auth": payload["auth"],
    }


async def test_fresh_vault_gets_a_relay_copy_of_the_chat_credential(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    main = await _chat_cred(db_session_factory, account_id, agent_id, PUBLIC_URL)
    vault = _Vault(f"daimon-mcp:{account_id}:{agent_id}", [main])
    seed = encode_seed(generate_signing_key())

    await ensure_agent_mcp_vault(
        _client(vault), account_id=account_id, agent_id=agent_id, jwt_secret=SECRET,
        public_url=PUBLIC_URL, now=NOW, session_factory=db_session_factory,
        relay_urls=(RELAY,),
    )  # fmt: skip

    main_now, relay = vault.by_url(PUBLIC_URL), vault.by_url(RELAY)
    assert relay["auth"]["token"] == main_now["auth"]["token"], "the relay copy is the same token"
    assert relay["metadata"][RELAY_METADATA_KEY] == "true", "the copy is marked as a relay copy"
    assert relay["metadata"]["daimon_chat_jti"] == main_now["metadata"]["daimon_chat_jti"], (
        "both slots record the same registered jti"
    )
    assert pyjwt.get_unverified_header(relay["auth"]["token"])["alg"] == "HS256", (
        "the runtime only ever holds Daimon's own chat token, never an identity token"
    )
    assert seed not in json.dumps(vault.creds), "no signing key reaches the vault"


async def test_refresh_without_relay_urls_keeps_an_existing_copy_in_step(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    main = await _chat_cred(db_session_factory, account_id, agent_id, PUBLIC_URL)
    stale = await _chat_cred(db_session_factory, account_id, agent_id, RELAY)
    stale["metadata"][RELAY_METADATA_KEY] = "true"
    vault = _Vault(f"daimon-mcp:{account_id}:{agent_id}", [main, stale])

    await refresh_agent_mcp_vault(
        _client(vault), vault_id="vlt_1", account_id=account_id, agent_id=agent_id,
        jwt_secret=SECRET, public_url=PUBLIC_URL, now=NOW,
        session_factory=db_session_factory,
        scope=Scope.legacy_host_authorized(call_site="test"),
    )  # fmt: skip

    assert vault.by_url(RELAY)["auth"]["token"] == vault.by_url(PUBLIC_URL)["auth"]["token"], (
        "a copy on an older token is re-minted with the main credential"
    )


async def test_fresh_main_and_matching_copy_write_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, agent_id = await _account(db_session_factory), uuid.uuid4()
    main = await _chat_cred(db_session_factory, account_id, agent_id, PUBLIC_URL)
    copy = {
        **main,
        "id": "vcrd_copy",
        "metadata": {**main["metadata"], RELAY_METADATA_KEY: "true"},
        "auth": {**main["auth"], "mcp_server_url": RELAY},
    }
    vault = _Vault(f"daimon-mcp:{account_id}:{agent_id}", [main, copy])

    await ensure_agent_mcp_vault(
        _client(vault), account_id=account_id, agent_id=agent_id, jwt_secret=SECRET,
        public_url=PUBLIC_URL, now=NOW, session_factory=db_session_factory,
        relay_urls=(RELAY,),
    )  # fmt: skip

    assert vault.writes == [], "a fresh credential and its copy are left alone"
