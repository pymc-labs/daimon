"""The grant write and the shared-token mirror racing for one vault URL.

MA keeps one credential per server URL in a vault: a second create at the URL
is a 409, and an update or delete of an id that is gone is a 404.
`put_mcp_oauth_credential` (after the callback has spent the flow) and
`mirror_credentials_into_vault` (every session create and remirror) both
write that slot without the per-(account, agent) advisory lock, so each must
survive the other landing between its list and its write. Found by the
VaultSlot TLA+ model (`formal/oauth`): configs GrantVsMirror and
GrantVsStaleMirror.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from daimon.core.agent_mcp_credentials import (
    METADATA_VERSION_KEY,
    ResolvedMcpCredential,
    mirror_credentials_into_vault,
)
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential

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
