"""Wire shapes of the OAuth discovery and token endpoints, as Pydantic.

Only the fields daimon reads. Unknown fields are ignored so a server that
advertises more than RFC 8414 / 9728 / 7591 / 6749 require still parses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

TokenEndpointAuthMethod = Literal["none", "client_secret_basic", "client_secret_post"]


class ProtectedResourceMetadata(BaseModel):
    """RFC 9728 — what the MCP server says about who authorizes it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str] | None = None


class AuthorizationServerMetadata(BaseModel):
    """RFC 8414 — the endpoints daimon drives."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes_supported: list[str] | None = None
    code_challenge_methods_supported: list[str] | None = None
    token_endpoint_auth_methods_supported: list[str] | None = None


class ClientRegistration(BaseModel):
    """RFC 7591 response — the client daimon registered for this deployment."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    client_id: str
    client_secret: str | None = None
    token_endpoint_auth_method: TokenEndpointAuthMethod = "none"


class TokenResponse(BaseModel):
    """RFC 6749 §5.1 — what the code exchange returned."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None
