"""`daimon.core.mcp_oauth.vault`: the mcp_oauth credential body and the replace-by-URL write."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_oauth.vault import build_mcp_oauth_auth, put_mcp_oauth_credential

_NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
_URL = "https://mcp.notion.com/mcp"


def test_build_mcp_oauth_auth_carries_refresh_block_for_a_public_client() -> None:
    auth = build_mcp_oauth_auth(
        mcp_server_url=_URL,
        tokens=TokenResponse(
            access_token="at", refresh_token="rt", expires_in=600, scope="default"
        ),
        client=ClientRegistration(client_id="cid"),
        token_endpoint="https://mcp.notion.com/token",
        resource="https://mcp.notion.com",
        now=_NOW,
    )
    assert auth["type"] == "mcp_oauth" and auth["access_token"] == "at"
    assert auth.get("expires_at") == _NOW + dt.timedelta(seconds=600), "expiry is now + expires_in"
    refresh = auth.get("refresh")
    assert refresh is not None
    assert refresh["token_endpoint_auth"] == {"type": "none"}, (
        "a public client refreshes unauthenticated"
    )
    assert refresh.get("scope") == "default", "scope rides the refresh block"
    assert refresh.get("resource") == "https://mcp.notion.com", "so does the resource"


def test_build_mcp_oauth_auth_omits_refresh_without_a_refresh_token() -> None:
    auth = build_mcp_oauth_auth(
        mcp_server_url=_URL,
        tokens=TokenResponse(access_token="at"),
        client=ClientRegistration(client_id="cid"),
        token_endpoint="https://mcp.notion.com/token",
        resource=None,
        now=_NOW,
    )
    assert "refresh" not in auth, "no refresh token means Anthropic cannot renew"
    assert auth.get("expires_at") == _NOW + dt.timedelta(days=365), (
        "nothing can renew it, so it must not lapse after an hour"
    )


def test_build_mcp_oauth_auth_passes_the_client_secret_for_a_confidential_client() -> None:
    auth = build_mcp_oauth_auth(
        mcp_server_url=_URL,
        tokens=TokenResponse(access_token="at", refresh_token="rt"),
        client=ClientRegistration(
            client_id="cid", client_secret="sec", token_endpoint_auth_method="client_secret_post"
        ),
        token_endpoint="https://mcp.notion.com/token",
        resource=None,
        now=_NOW,
    )
    refresh = auth.get("refresh")
    assert refresh is not None
    assert refresh["token_endpoint_auth"] == {"type": "client_secret_post", "client_secret": "sec"}


async def test_put_mcp_oauth_credential_replaces_the_credential_at_the_same_url() -> None:
    deleted: list[str] = []
    created: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/vaults/vlt_1/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_old",
                            "type": "vault_credential",
                            "vault_id": "vlt_1",
                            "auth": {"type": "static_bearer", "mcp_server_url": _URL + "/"},
                            "created_at": "2026-09-01T00:00:00Z",
                            "updated_at": "2026-09-01T00:00:00Z",
                            "archived_at": None,
                            "display_name": None,
                            "metadata": None,
                        },
                        {
                            "id": "vcrd_other",
                            "type": "vault_credential",
                            "vault_id": "vlt_1",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": "https://mcp.linear.app/mcp",
                            },
                            "created_at": "2026-09-01T00:00:00Z",
                            "updated_at": "2026-09-01T00:00:00Z",
                            "archived_at": None,
                            "display_name": None,
                            "metadata": None,
                        },
                    ],
                    "has_more": False,
                    "first_id": "vcrd_old",
                    "last_id": "vcrd_other",
                },
            )
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": deleted[-1], "type": "vault_credential_deleted"})
        if request.method == "POST" and path == "/v1/vaults/vlt_1/credentials":
            body = json.loads(request.content)
            created.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "vault_credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "mcp_oauth", "mcp_server_url": _URL},
                    "created_at": "2026-09-15T12:00:00Z",
                    "updated_at": "2026-09-15T12:00:00Z",
                    "archived_at": None,
                    "display_name": body.get("display_name"),
                    "metadata": None,
                },
            )
        return httpx.Response(404)

    anthropic = AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    credential_id = await put_mcp_oauth_credential(
        anthropic,
        vault_id="vlt_1",
        mcp_server_url=_URL,
        tokens=TokenResponse(access_token="at", refresh_token="rt", expires_in=60),
        client=ClientRegistration(client_id="cid"),
        token_endpoint="https://mcp.notion.com/token",
        resource=None,
        now=_NOW,
    )
    assert credential_id == "vcrd_new"
    assert deleted == ["vcrd_old"], "only the credential at the same URL is replaced"
    assert created[0]["auth"]["type"] == "mcp_oauth"
    assert created[0]["auth"]["refresh"]["refresh_token"] == "rt"
