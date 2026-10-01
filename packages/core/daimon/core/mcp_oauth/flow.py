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
        "client_id": client.client_id,
    }
    if resource:
        form["resource"] = resource
    headers = {"Accept": "application/json"}
    if client.client_secret is not None:
        if client.token_endpoint_auth_method == "client_secret_basic":
            basic = base64.b64encode(f"{client.client_id}:{client.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            form["client_secret"] = client.client_secret
    response = await http.post(token_endpoint, data=form, headers=headers)
    if response.status_code != 200:
        raise McpOAuthFlowError(
            f"token exchange at {token_endpoint} failed: "
            f"{response.status_code} {response.text[:300]}"
        )
    try:
        return TokenResponse.model_validate(response.json())
    except ValueError as err:
        # Never include the body: a token response is a secret.
        raise McpOAuthFlowError(
            f"token exchange at {token_endpoint} returned an unusable body"
        ) from err
