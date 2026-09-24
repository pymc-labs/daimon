"""Write an OAuth grant into one caller's per-agent MA vault.

Mirrors `mcp_vault.add_external_mcp_credential`'s replace-by-URL shape for
the `mcp_oauth` credential type. With a refresh token and the token endpoint
Anthropic renews the access token itself; without one MA uses the access
token until it expires and then reports `mcp_authentication_failed_error`,
which the degraded-turn path names so the person can reconnect.
"""

from __future__ import annotations

import contextlib
import datetime as dt

from anthropic import AsyncAnthropic, ConflictError, NotFoundError
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from anthropic.types.beta.vaults.beta_managed_agents_mcp_oauth_create_params import (
    BetaManagedAgentsMCPOAuthCreateParams,
)
from anthropic.types.beta.vaults.beta_managed_agents_mcp_oauth_refresh_params import (
    BetaManagedAgentsMCPOAuthRefreshParams,
    TokenEndpointAuth,
)
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_vault import same_server_url

# Anthropic wants a bound expiry. A server that omits `expires_in` but hands
# out a refresh token gets a short lifetime, since Anthropic renews it anyway;
# one that omits both issued a long-lived token, and a short guess would make
# MA retire a working credential after an hour with no way to renew it.
_REFRESHABLE_DEFAULT_LIFETIME = dt.timedelta(hours=1)
_UNREFRESHABLE_DEFAULT_LIFETIME = dt.timedelta(days=365)

# Defence in depth only: callers hold the per-(account, agent) vault lock, so
# no daimon writer lands in the slot mid-write. The retry covers one outside
# the lock, such as a process still on an older release during a deploy.
_WRITE_ATTEMPTS = 3


def _token_endpoint_auth(client: ClientRegistration) -> TokenEndpointAuth:
    if client.client_secret is None or client.token_endpoint_auth_method == "none":
        return {"type": "none"}
    if client.token_endpoint_auth_method == "client_secret_post":
        return {"type": "client_secret_post", "client_secret": client.client_secret}
    return {"type": "client_secret_basic", "client_secret": client.client_secret}


def build_mcp_oauth_auth(
    *,
    mcp_server_url: str,
    tokens: TokenResponse,
    client: ClientRegistration,
    token_endpoint: str,
    resource: str | None,
    now: dt.datetime,
) -> BetaManagedAgentsMCPOAuthCreateParams:
    """Pure: the `auth` body for `vaults.credentials.create`."""
    if tokens.expires_in is not None:
        lifetime = dt.timedelta(seconds=tokens.expires_in)
    elif tokens.refresh_token is not None:
        lifetime = _REFRESHABLE_DEFAULT_LIFETIME
    else:
        lifetime = _UNREFRESHABLE_DEFAULT_LIFETIME
    auth: BetaManagedAgentsMCPOAuthCreateParams = {
        "type": "mcp_oauth",
        "mcp_server_url": mcp_server_url,
        "access_token": tokens.access_token,
        "expires_at": now + lifetime,
    }
    if tokens.refresh_token is not None:
        refresh: BetaManagedAgentsMCPOAuthRefreshParams = {
            "client_id": client.client_id,
            "refresh_token": tokens.refresh_token,
            "token_endpoint": token_endpoint,
            "token_endpoint_auth": _token_endpoint_auth(client),
        }
        if tokens.scope:
            refresh["scope"] = tokens.scope
        if resource:
            refresh["resource"] = resource
        auth["refresh"] = refresh
    return auth


async def put_mcp_oauth_credential(
    anthropic: AsyncAnthropic,
    *,
    vault_id: str,
    mcp_server_url: str,
    tokens: TokenResponse,
    client: ClientRegistration,
    token_endpoint: str,
    resource: str | None,
    now: dt.datetime,
) -> str:
    """Replace whatever credential the vault holds for the URL; return the new id.

    The caller holds the per-(account, agent) vault lock
    (`mcp_vault.hold_agent_vault_lock`), as every writer that replaces a
    URL's credential does. A turn's `mirror_credentials_into_vault` therefore
    either finished before this lists the slot or waits and then sees the
    grant and leaves the URL alone, however many turns are mirroring.

    A writer outside the lock could still land between the list and the
    create (the create is then a 409) or delete a listed credential first
    (the delete is then a 404). The slot is re-read and replaced again, up
    to `_WRITE_ATTEMPTS` times; that retry is not what makes the common
    case safe, the lock is.
    """
    auth = build_mcp_oauth_auth(
        mcp_server_url=mcp_server_url,
        tokens=tokens,
        client=client,
        token_endpoint=token_endpoint,
        resource=resource,
        now=now,
    )
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        # Collect first: deleting while the list paginates can skip an entry.
        stale = [
            existing.id
            async for existing in anthropic.beta.vaults.credentials.list(vault_id=vault_id)
            if not isinstance(existing.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
            and same_server_url(existing.auth.mcp_server_url, mcp_server_url)
        ]
        for credential_id in stale:
            # Already gone: another writer removed it, which is what we wanted.
            with contextlib.suppress(NotFoundError):
                await anthropic.beta.vaults.credentials.delete(credential_id, vault_id=vault_id)
        try:
            created = await anthropic.beta.vaults.credentials.create(
                vault_id=vault_id,
                auth=auth,
                display_name=f"oauth:{mcp_server_url}"[:255],
            )
        except ConflictError:
            # A concurrent writer filled the slot after our delete; re-read it.
            if attempt == _WRITE_ATTEMPTS:
                raise
            continue
        return created.id
    raise AssertionError("unreachable: the last attempt returns or raises")
