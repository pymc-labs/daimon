"""PKCE, dynamic client registration, the authorize URL and the code exchange.

`generate_pkce` takes its randomness as an argument and
`build_authorization_url` is a total function of its inputs, so both are unit
tested without I/O. `register_client` and `exchange_authorization_code` are
one HTTP round trip each on an injected client and raise
`McpOAuthFlowError` with the server's status and body on anything but
success — an OAuth server's error body is the only diagnostic there is.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

import httpx
from daimon.core.errors import DaimonError
from daimon.core.mcp_oauth.models import (
    AuthorizationServerMetadata,
    ClientRegistration,
    TokenEndpointAuthMethod,
    TokenResponse,
)


class McpOAuthFlowError(DaimonError):
    """Registration or the code exchange was refused by the OAuth server."""


class McpOAuthTokenRefusedError(McpOAuthFlowError):
    """The token endpoint answered the code exchange with an error status."""

    def __init__(self, message: str, *, error_code: str | None) -> None:
        super().__init__(message)
        self.error_code = error_code
        """The RFC 6749 `error` field, e.g. `invalid_client` for a wrong secret."""


@dataclass(frozen=True, slots=True)
class Pkce:
    code_verifier: str
    code_challenge: str


def generate_pkce(*, verifier: str | None = None) -> Pkce:
    """RFC 7636 S256 pair; `verifier` is injectable for tests."""
    code_verifier = verifier if verifier is not None else secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return Pkce(
        code_verifier=code_verifier,
        code_challenge=base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None,
    resource: str | None,
) -> str:
    """The browser URL for one authorization-code request with PKCE.

    `resource` (RFC 8707) names the MCP server the token is for; MCP clients
    send it whenever the server published protected-resource metadata.
    """
    query: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        query["scope"] = scope
    if resource:
        query["resource"] = resource
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{urlencode(query)}"


def requested_scope(scopes: list[str] | None, server_scopes: list[str] | None) -> str | None:
    """A pre-registered client's scopes, plus `offline_access` when the server offers it.

    Some authorization servers issue a refresh token only for `offline_access`;
    without one the grant lapses with its first access token. Dynamically
    registered clients do not get it, so those servers see the request they
    always did. No scope stays no scope, so the server's default set is not
    narrowed to that one.
    """
    if not scopes:
        return None
    requested = list(scopes)
    if "offline_access" in (server_scopes or []) and "offline_access" not in requested:
        requested.append("offline_access")
    return " ".join(requested)


def refresh_scope(granted: str | None, requested: str | None) -> str | None:
    """The scope a refresh asks for: the granted one, keeping a requested `offline_access`.

    Token responses often leave `offline_access` out of `scope` even when it
    was granted, and a refresh limited to that echo would stop yielding new
    refresh tokens.
    """
    if granted is None or requested is None:
        return granted
    scopes = granted.split()
    if "offline_access" in requested.split() and "offline_access" not in scopes:
        scopes.append("offline_access")
    return " ".join(scopes)


def pick_token_endpoint_auth_method(
    metadata: AuthorizationServerMetadata,
) -> TokenEndpointAuthMethod:
    """Prefer a public client; a secret is only asked for when `none` is not offered."""
    offered = metadata.token_endpoint_auth_methods_supported or ["client_secret_basic"]
    for method in ("none", "client_secret_post", "client_secret_basic"):
        if method in offered:
            return method
    return "client_secret_basic"


async def register_client(
    http: httpx.AsyncClient,
    metadata: AuthorizationServerMetadata,
    *,
    redirect_uri: str,
    client_name: str,
    scope: str | None,
) -> ClientRegistration:
    """RFC 7591 dynamic registration of one client for this deployment."""
    if metadata.registration_endpoint is None:
        raise McpOAuthFlowError(
            f"{metadata.issuer} offers no dynamic client registration; "
            "daimon cannot register itself as an OAuth client there"
        )
    body: dict[str, object] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": pick_token_endpoint_auth_method(metadata),
    }
    if scope:
        body["scope"] = scope
    response = await http.post(metadata.registration_endpoint, json=body)
    if response.status_code not in (200, 201):
        raise McpOAuthFlowError(
            f"client registration at {metadata.registration_endpoint} failed: "
            f"{response.status_code} {response.text[:300]}"
        )
    try:
        return ClientRegistration.model_validate(response.json())
    except ValueError as err:
        # The body stays out of the message: registration echoes what we sent
        # plus the server's own fields, none of which belong in a log line.
        raise McpOAuthFlowError(
            f"client registration at {metadata.registration_endpoint} returned an unusable body"
        ) from err


def _oauth_error_code(response: httpx.Response) -> str | None:
    """The `error` field of an OAuth error body, or None when there is none."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        code = cast("dict[str, object]", body).get("error")
        if isinstance(code, str):
            return code[:80]
    return None


async def exchange_authorization_code(
    http: httpx.AsyncClient,
    *,
    token_endpoint: str,
    code: str,
    code_verifier: str,
    client: ClientRegistration,
    redirect_uri: str,
    resource: str | None,
) -> TokenResponse:
    """Trade the callback's `code` for tokens, authenticating as registered."""
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    return await _token_request(
        http, token_endpoint=token_endpoint, form=form, client=client, resource=resource
    )


async def refresh_access_token(
    http: httpx.AsyncClient,
    *,
    token_endpoint: str,
    refresh_token: str,
    client: ClientRegistration,
    scope: str | None,
    resource: str | None,
) -> TokenResponse:
    """Trade a refresh token for a new access token, asking for no more than `scope`."""
    form: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if scope:
        form["scope"] = scope
    return await _token_request(
        http, token_endpoint=token_endpoint, form=form, client=client, resource=resource
    )


async def _token_request(
    http: httpx.AsyncClient,
    *,
    token_endpoint: str,
    form: dict[str, str],
    client: ClientRegistration,
    resource: str | None,
) -> TokenResponse:
    form = {**form, "client_id": client.client_id}
    if resource:
        form["resource"] = resource
    headers = {"Accept": "application/json"}
    if client.client_secret is not None:
        if client.token_endpoint_auth_method == "client_secret_basic":
            basic = base64.b64encode(f"{client.client_id}:{client.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            form["client_secret"] = client.client_secret
    grant = "token refresh" if form["grant_type"] == "refresh_token" else "token exchange"
    response = await http.post(token_endpoint, data=form, headers=headers)
    if response.status_code != 200:
        raise McpOAuthTokenRefusedError(
            f"{grant} at {token_endpoint} failed: {response.status_code} {response.text[:300]}",
            error_code=_oauth_error_code(response),
        )
    try:
        return TokenResponse.model_validate(response.json())
    except ValueError as err:
        # Never include the body: a token response is a secret.
        raise McpOAuthFlowError(f"{grant} at {token_endpoint} returned an unusable body") from err
