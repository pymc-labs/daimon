"""`daimon.core.mcp_oauth.discovery` against a mocked MCP server and its metadata."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.mcp_oauth.discovery import (
    McpOAuthDiscoveryError,
    authorization_server_urls,
    discover_authorization_server,
    probe_mcp_server,
    protected_resource_urls,
)
from daimon.core.mcp_oauth.urls import McpUrlError

_MCP_URL = "https://mcp.notion.com/mcp"
_PRM = {
    "resource": "https://mcp.notion.com",
    "authorization_servers": ["https://mcp.notion.com"],
    "scopes_supported": ["default"],
}
_AS = {
    "issuer": "https://mcp.notion.com",
    "authorization_endpoint": "https://mcp.notion.com/authorize",
    "token_endpoint": "https://mcp.notion.com/token",
    "registration_endpoint": "https://mcp.notion.com/register",
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
}


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def test_protected_resource_urls_put_the_advertised_url_first_then_path_then_root() -> None:
    urls = protected_resource_urls(_MCP_URL, "https://mcp.notion.com/.well-known/x")
    assert urls == [
        "https://mcp.notion.com/.well-known/x",
        "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.notion.com/.well-known/oauth-protected-resource",
    ], "WWW-Authenticate wins, then the path-aware document, then the root one"


def test_protected_resource_urls_ignore_an_advertised_url_off_the_servers_host() -> None:
    """A hostile 401 must not steer the deployment into fetching an arbitrary URL."""
    for advertised in ("https://evil.example/.well-known/x", "http://mcp.notion.com/.well-known/x"):
        urls = protected_resource_urls(_MCP_URL, advertised)
        assert advertised not in urls, advertised
        assert urls[0] == "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp"


def test_authorization_server_urls_are_path_aware_for_an_issuer_with_a_path() -> None:
    urls = authorization_server_urls("https://auth.example.com/tenant")
    assert urls[0] == "https://auth.example.com/.well-known/oauth-authorization-server/tenant"
    assert urls[-1] == "https://auth.example.com/tenant/.well-known/openid-configuration"


async def test_probe_reads_the_resource_metadata_hint_from_a_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == _MCP_URL
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.notion.com/.well-known/'
                    'oauth-protected-resource", error="invalid_token"'
                )
            },
        )

    probe = await probe_mcp_server(_client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL)
    assert probe.rejects_credentials, "a 401 is a credential rejection"
    assert probe.resource_metadata_url == (
        "https://mcp.notion.com/.well-known/oauth-protected-resource"
    ), "the advertised metadata URL is extracted"


async def test_probe_sends_the_mcp_protocol_version_header() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("mcp-protocol-version"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    await probe_mcp_server(_client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL)
    assert seen == ["2025-06-18"], "the initialize probe declares its protocol version"


async def test_probe_sends_the_bearer_token_when_given_one() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    probe = await probe_mcp_server(
        _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL, bearer_token="tok"
    )
    assert seen == ["Bearer tok"], "the token under test rides the Authorization header"
    assert probe.accepts_bearer, "a 2xx means the server took the token"


async def test_discover_resolves_protected_resource_then_authorization_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(404)
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json=_PRM)
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_AS)
        return httpx.Response(404)

    found = await discover_authorization_server(
        _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
    )
    assert found.resource is not None and found.resource.resource == "https://mcp.notion.com"
    assert found.authorization_server.registration_endpoint == "https://mcp.notion.com/register"


async def test_discover_falls_back_to_the_server_host_when_no_resource_document() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_AS)
        return httpx.Response(404)

    found = await discover_authorization_server(
        _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
    )
    assert found.resource is None, "no protected-resource document was published"
    assert found.authorization_server.token_endpoint == "https://mcp.notion.com/token"


async def test_discover_raises_naming_the_urls_tried_when_nothing_answers() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(McpOAuthDiscoveryError, match="oauth-authorization-server"):
        await discover_authorization_server(
            _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
        )


async def test_discover_reports_a_malformed_metadata_document_as_discovery_failure() -> None:
    """A SPA catch-all answering 200 text/html is not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, text="<!doctype html><title>app</title>")
        return httpx.Response(404)

    with pytest.raises(McpOAuthDiscoveryError, match="not a valid ProtectedResourceMetadata"):
        await discover_authorization_server(
            _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
        )


async def test_discover_skips_an_issuer_that_is_not_a_public_https_host() -> None:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={**_PRM, "authorization_servers": ["http://10.0.0.5", "https://[::1]"]},
            )
        return httpx.Response(404)

    with pytest.raises(McpOAuthDiscoveryError, match="not a public https issuer"):
        await discover_authorization_server(
            _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
        )
    assert all("10.0.0.5" not in url and "::1" not in url for url in fetched), (
        "a hostile resource document must not steer a fetch into the network"
    )


async def test_discover_refuses_metadata_whose_endpoints_are_not_public_https() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200, json={**_AS, "authorization_endpoint": "http://mcp.notion.com/authorize"}
            )
        return httpx.Response(404)

    with pytest.raises(McpUrlError, match="authorization_endpoint"):
        await discover_authorization_server(
            _client(httpx.MockTransport(handler)), mcp_server_url=_MCP_URL
        )


async def test_probe_refuses_to_contact_a_private_address() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with pytest.raises(McpUrlError):
        await probe_mcp_server(
            _client(httpx.MockTransport(handler)), mcp_server_url="https://10.0.0.5:6379/"
        )
    assert calls == [], "the request is refused before it leaves"
