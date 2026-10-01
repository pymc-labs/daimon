"""Authorization-code flow for MCP servers that only accept OAuth (Notion, Slack, …).

Managed Agents holds the tokens (an `mcp_oauth` vault credential, refreshed by
Anthropic) and connects to the server at session time; daimon's only job is
the browser half — discovery, dynamic client registration, PKCE, the code
exchange — and writing the result into the caller's vault. Grants are per
person: each member connects their own account, and the credential lands in
that member's per-agent vault only.

`models` and `flow.build_authorization_url` are pure; `discovery`, `flow`'s
HTTP calls and `vault` are the shell, with the `httpx.AsyncClient` and the
Anthropic client injected.
"""

from daimon.core.mcp_oauth.complete import (
    McpOAuthCompletion,
    McpOAuthIncompleteFlowError,
    complete_mcp_oauth_flow,
)
from daimon.core.mcp_oauth.discovery import (
    McpProbe,
    McpTokenProbe,
    OAuthDiscovery,
    discover_authorization_server,
    probe_bearer_token,
    probe_mcp_server,
)
from daimon.core.mcp_oauth.flow import (
    Pkce,
    build_authorization_url,
    exchange_authorization_code,
    generate_pkce,
    register_client,
)
from daimon.core.mcp_oauth.handshake import (
    FLOW_TTL,
    INVITE_BUTTON_LABEL,
    PreparedAuthorization,
    begin_mcp_oauth_flow,
    callback_url,
    invite_copy,
    prepare_authorization,
    start_url,
)
from daimon.core.mcp_oauth.models import (
    AuthorizationServerMetadata,
    ClientRegistration,
    ProtectedResourceMetadata,
    TokenResponse,
)
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_host, assert_public_https_url
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential

__all__ = [
    "FLOW_TTL",
    "INVITE_BUTTON_LABEL",
    "AuthorizationServerMetadata",
    "ClientRegistration",
    "McpOAuthCompletion",
    "McpOAuthIncompleteFlowError",
    "McpProbe",
    "McpTokenProbe",
    "McpUrlError",
    "OAuthDiscovery",
    "Pkce",
    "PreparedAuthorization",
    "ProtectedResourceMetadata",
    "TokenResponse",
    "assert_public_host",
    "assert_public_https_url",
    "begin_mcp_oauth_flow",
    "build_authorization_url",
    "callback_url",
    "complete_mcp_oauth_flow",
    "discover_authorization_server",
    "exchange_authorization_code",
    "generate_pkce",
    "invite_copy",
    "prepare_authorization",
    "probe_bearer_token",
    "probe_mcp_server",
    "put_mcp_oauth_credential",
    "register_client",
    "start_url",
]
