"""list_credentials: transport-level mock for vault credential listing."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.tools.vault import VaultCredentialSummary, _list_credentials_impl
from daimon.core.mcp_vault import ensure_agent_mcp_vault
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

VAULT_ID = "vault_abc"
ACCOUNT_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=ACCOUNT_ID, tenant_id=TENANT_ID, role=Role.ADMIN, agent_id=AGENT_ID
    )


def _make_client(handler: httpx.MockTransport) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(transport=handler),
    )


def _vault_list_response() -> dict[str, object]:
    """Minimal response for GET /v1/beta/vaults (list)."""
    return {
        "data": [
            {
                "id": VAULT_ID,
                "type": "vault",
                "display_name": f"daimon-mcp:{ACCOUNT_ID}:{AGENT_ID}",
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": VAULT_ID,
        "last_id": VAULT_ID,
    }


def _credential_list_response() -> dict[str, object]:
    """Response for GET /v1/beta/vaults/{id}/credentials."""
    return {
        "data": [
            {
                "id": "cred_1",
                "type": "credential",
                "vault_id": VAULT_ID,
                "mcp_server_url": "https://x/mcp",
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
                "archived_at": None,
                "metadata": {},
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": "https://x/mcp",
                    "token": "SECRET_TOKEN_SHOULD_NOT_APPEAR",
                },
            }
        ],
        "has_more": False,
        "first_id": "cred_1",
        "last_id": "cred_1",
    }


async def test_list_credentials_returns_safe_projection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/vaults/" in str(request.url) and "/credentials" in str(request.url):
            return httpx.Response(200, json=_credential_list_response())
        if "/vaults" in str(request.url):
            return httpx.Response(200, json=_vault_list_response())
        return httpx.Response(404)

    client = _make_client(httpx.MockTransport(handler))
    result = await _list_credentials_impl(client, _auth())

    assert len(result) == 1, "should return one credential"
    cred = result[0]
    assert isinstance(cred, VaultCredentialSummary), "should return VaultCredentialSummary"
    assert cred.id == "cred_1", "should preserve credential id"
    assert cred.vault_id == VAULT_ID, "should preserve vault id"

    cred_dict = cred.model_dump()
    assert "token" not in json.dumps(cred_dict), "secret token must not appear in safe projection"


async def test_list_credentials_auth_strips_token_from_auth_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/vaults/" in str(request.url) and "/credentials" in str(request.url):
            return httpx.Response(200, json=_credential_list_response())
        if "/vaults" in str(request.url):
            return httpx.Response(200, json=_vault_list_response())
        return httpx.Response(404)

    client = _make_client(httpx.MockTransport(handler))
    result = await _list_credentials_impl(client, _auth())

    cred = result[0]
    assert cred.auth is not None, "should include auth summary"
    assert cred.auth.type == "static_bearer", "should preserve auth type"
    assert cred.auth.mcp_server_url == "https://x/mcp", "should preserve server URL"
    auth_dict = cred.auth.model_dump()
    assert "token" not in auth_dict, "auth block must not carry token"


async def test_list_credentials_raises_tool_error_when_no_vault_exists() -> None:
    empty_list: dict[str, object] = {
        "data": [],
        "has_more": False,
        "first_id": None,
        "last_id": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/vaults" in str(request.url):
            return httpx.Response(200, json=empty_list)
        return httpx.Response(404)

    client = _make_client(httpx.MockTransport(handler))
    with pytest.raises(ToolError, match="no MCP vault found"):
        await _list_credentials_impl(client, _auth())


async def test_list_credentials_resolves_vault_provisioned_by_ensure_agent_mcp_vault(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The reader must never hand-name the vault: it has to agree with
    whatever the real provisioning helper wrote, not a fake built to match.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    vaults: dict[str, dict[str, object]] = {}
    credentials: dict[str, list[dict[str, object]]] = {}

    def create_vault(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        body = json_body(request)
        vault_id = f"vault_{uuid.uuid4().hex[:8]}"
        vault: dict[str, object] = {
            "id": vault_id,
            "type": "vault",
            "display_name": body["display_name"],
            "created_at": "2026-04-24T00:00:00Z",
            "updated_at": "2026-04-24T00:00:00Z",
        }
        vaults[vault_id] = vault
        credentials[vault_id] = []
        return httpx.Response(200, json=vault)

    def list_vaults(_request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        return list_response(list(vaults.values()))

    def create_credential(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        vault_id = match.group("vault_id")
        body = json_body(request)
        cred_id = f"cred_{uuid.uuid4().hex[:8]}"
        cred: dict[str, object] = {
            "id": cred_id,
            "type": "credential",
            "vault_id": vault_id,
            "created_at": "2026-04-24T00:00:00Z",
            "updated_at": "2026-04-24T00:00:00Z",
            "archived_at": None,
            "metadata": {},
            "auth": body["auth"],
        }
        credentials[vault_id].append(cred)
        return httpx.Response(200, json=cred)

    def list_credentials_route(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        vault_id = match.group("vault_id")
        return list_response(credentials.get(vault_id, []))

    router = MARouter()
    router.add("POST", r"/v1/vaults", create_vault)
    router.add("GET", r"/v1/vaults", list_vaults)
    router.add("POST", r"/v1/vaults/(?P<vault_id>[^/]+)/credentials", create_credential)
    router.add("GET", r"/v1/vaults/(?P<vault_id>[^/]+)/credentials", list_credentials_route)

    client = build_fake_anthropic(router.dispatch)

    await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=b"a" * 32,
        public_url="https://mcp.example.com/mcp",
        now=dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        session_factory=committing_sessionmaker,
    )

    auth = AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, agent_id=agent_id
    )
    result = await _list_credentials_impl(client, auth)

    assert len(result) == 1, "must find the credential the real provisioning path wrote"
    cred = result[0]
    assert isinstance(cred, VaultCredentialSummary), "should return VaultCredentialSummary"
    cred_dict = cred.model_dump()
    assert "token" not in json.dumps(cred_dict), "secret token must not appear in safe projection"
