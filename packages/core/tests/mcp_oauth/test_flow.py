"""`daimon.core.mcp_oauth.flow`: PKCE, the authorize URL, registration, exchange."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.core.mcp_oauth.flow import (
    McpOAuthFlowError,
    build_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    pick_token_endpoint_auth_method,
    register_client,
)
from daimon.core.mcp_oauth.models import AuthorizationServerMetadata, ClientRegistration

_AS = AuthorizationServerMetadata(
    issuer="https://mcp.notion.com",
    authorization_endpoint="https://mcp.notion.com/authorize",
    token_endpoint="https://mcp.notion.com/token",
    registration_endpoint="https://mcp.notion.com/register",
    token_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post", "none"],
)


def test_generate_pkce_derives_the_s256_challenge_from_the_verifier() -> None:
    pkce = generate_pkce(verifier="a" * 64)
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"a" * 64).digest()).decode().rstrip("=")
    assert pkce.code_challenge == expected, "challenge must be base64url(sha256(verifier))"
    assert generate_pkce().code_verifier != generate_pkce().code_verifier, "fresh per call"


def test_build_authorization_url_carries_pkce_state_and_resource() -> None:
    url = build_authorization_url(
        _AS.authorization_endpoint,
        client_id="cid",
        redirect_uri="https://d.example/oauth/mcp/callback",
        state="st",
        code_challenge="ch",
        scope="default",
        resource="https://mcp.notion.com",
    )
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"] and query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["st"] and query["code_challenge"] == ["ch"]
    assert query["resource"] == ["https://mcp.notion.com"], "RFC 8707 resource indicator"
    assert query["scope"] == ["default"]


def test_pick_token_endpoint_auth_method_prefers_a_public_client() -> None:
    assert pick_token_endpoint_auth_method(_AS) == "none"
    secret_only = _AS.model_copy(
        update={"token_endpoint_auth_methods_supported": ["client_secret_basic"]}
    )
    assert pick_token_endpoint_auth_method(secret_only) == "client_secret_basic"


async def test_register_client_posts_rfc7591_metadata_and_parses_the_client() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(
            201, json={"client_id": "cid", "token_endpoint_auth_method": "none", "extra": 1}
        )

    client = await register_client(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        _AS,
        redirect_uri="https://d.example/oauth/mcp/callback",
        client_name="daimon",
        scope=None,
    )
    assert client == ClientRegistration(client_id="cid", token_endpoint_auth_method="none")
    assert bodies[0]["redirect_uris"] == ["https://d.example/oauth/mcp/callback"]
    assert bodies[0]["grant_types"] == ["authorization_code", "refresh_token"]


async def test_register_client_refuses_a_server_without_dynamic_registration() -> None:
    no_dcr = _AS.model_copy(update={"registration_endpoint": None})
    with pytest.raises(McpOAuthFlowError, match="no dynamic client registration"):
        await register_client(
            httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500))),
            no_dcr,
            redirect_uri="https://d.example/cb",
            client_name="daimon",
            scope=None,
        )


async def test_exchange_sends_the_verifier_and_uses_basic_auth_for_a_confidential_client() -> None:
    forms: list[dict[str, list[str]]] = []
    auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forms.append(parse_qs(request.content.decode()))
        auth_headers.append(request.headers.get("authorization"))
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    tokens = await exchange_authorization_code(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_endpoint=_AS.token_endpoint,
        code="code123",
        code_verifier="verifier",
        client=ClientRegistration(
            client_id="cid", client_secret="sec", token_endpoint_auth_method="client_secret_basic"
        ),
        redirect_uri="https://d.example/cb",
        resource="https://mcp.notion.com",
    )
    assert tokens.refresh_token == "rt" and tokens.expires_in == 3600
    assert forms[0]["code_verifier"] == ["verifier"] and forms[0]["resource"] == [
        "https://mcp.notion.com"
    ]
    assert auth_headers[0] is not None and auth_headers[0].startswith("Basic "), (
        "client_secret_basic must authenticate the token request"
    )
    assert "client_secret" not in forms[0], "basic auth means no secret in the form"


async def test_exchange_raises_with_the_server_body_on_refusal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(McpOAuthFlowError, match="invalid_grant"):
        await exchange_authorization_code(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            token_endpoint=_AS.token_endpoint,
            code="bad",
            code_verifier="v",
            client=ClientRegistration(client_id="cid"),
            redirect_uri="https://d.example/cb",
            resource=None,
        )


async def test_exchange_wraps_an_unusable_token_body_without_repeating_it() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer", "secret_thing": "s3cr3t"})

    with pytest.raises(McpOAuthFlowError, match="unusable body") as excinfo:
        await exchange_authorization_code(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            token_endpoint=_AS.token_endpoint,
            code="c",
            code_verifier="v",
            client=ClientRegistration(client_id="cid"),
            redirect_uri="https://d.example/cb",
            resource=None,
        )
    assert "s3cr3t" not in str(excinfo.value), "a token response body never reaches a log line"
    assert excinfo.value.__cause__ is not None, "the parse error stays attached as the cause"
