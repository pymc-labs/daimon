"""Microsoft identity platform (Entra v2) for Connect Microsoft.

Authorize URL, code exchange and refresh as a confidential client
(client_secret_post) against the configured tenant's endpoints, and the
identity check that binds the signed-in account to the Teams person. All I/O
goes through an injected `httpx.AsyncClient`; callers are the catch site.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.errors import DaimonError

# Delegated Graph permissions; `openid` and `profile` make the token response
# carry an id_token with the `oid` the identity check reads.
MICROSOFT_SCOPES: tuple[str, ...] = (
    "openid",
    "profile",
    "User.Read",
    "offline_access",
    "Files.ReadWrite.All",
    "Sites.Read.All",
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Notes.ReadWrite.All",
)
# Errors meaning the stored grant can no longer be used without the person
# signing in again (revoked, expired, or a Conditional Access re-prompt).
_RECONNECT_ERRORS = frozenset({"invalid_grant", "interaction_required"})
# Errors about the app registration itself (a wrong or expired client secret, a
# wrong tenant): no person can fix these by reconnecting.
_APP_ERRORS = frozenset({"invalid_client", "unauthorized_client"})


class MicrosoftIdentityError(DaimonError):
    """The token endpoint refused, or answered without what Daimon needs."""


class MicrosoftGrantRevoked(MicrosoftIdentityError):
    """The refresh token no longer works; the person must connect again."""


class MicrosoftAppRejected(MicrosoftIdentityError):
    """Entra refused this deployment's app registration; an administrator must fix it."""


class MicrosoftIdentityMismatch(MicrosoftIdentityError):
    """The signed-in account is not the Teams person's account in the configured tenant."""


@dataclass(frozen=True, slots=True)
class MicrosoftTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str
    id_token: str | None


def _endpoint(config: MicrosoftAccountsSettings, name: str) -> str:
    return f"{str(config.authority).rstrip('/')}/{config.tenant_id}/oauth2/v2.0/{name}"


def build_authorize_url(
    config: MicrosoftAccountsSettings,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...] = MICROSOFT_SCOPES,
) -> str:
    params = {
        "client_id": config.client_id or "",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{_endpoint(config, 'authorize')}?{urlencode(params)}"


async def _token_request(
    client: httpx.AsyncClient, config: MicrosoftAccountsSettings, data: dict[str, str]
) -> MicrosoftTokens:
    secret = config.client_secret.get_secret_value() if config.client_secret else ""
    response = await client.post(
        _endpoint(config, "token"),
        data={**data, "client_id": config.client_id or "", "client_secret": secret},
        timeout=10.0,
    )
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        # Only the error code: descriptions can echo request details.
        error = str(payload.get("error") or f"http_{response.status_code}")
        if error in _RECONNECT_ERRORS:
            raise MicrosoftGrantRevoked(error)
        raise (MicrosoftAppRejected if error in _APP_ERRORS else MicrosoftIdentityError)(error)
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MicrosoftIdentityError("token response carried no access_token")
    refresh_token = payload.get("refresh_token")
    id_token = payload.get("id_token")
    return MicrosoftTokens(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_in=int(payload.get("expires_in") or 0),
        scope=str(payload.get("scope") or ""),
        id_token=id_token if isinstance(id_token, str) else None,
    )


async def exchange_code(
    client: httpx.AsyncClient,
    config: MicrosoftAccountsSettings,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = MICROSOFT_SCOPES,
) -> MicrosoftTokens:
    return await _token_request(
        client,
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
        },
    )


async def refresh_tokens(
    client: httpx.AsyncClient, config: MicrosoftAccountsSettings, *, refresh_token: str
) -> MicrosoftTokens:
    """Raises `MicrosoftGrantRevoked` when the person must connect again."""
    return await _token_request(
        client,
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(MICROSOFT_SCOPES),
        },
    )


def id_token_claims(id_token: str) -> dict[str, Any]:
    # No signature check: the id_token came straight from the token endpoint
    # over TLS in this same exchange (OpenID Connect Core 3.1.3.7).
    parts = id_token.split(".")
    if len(parts) != 3:
        raise MicrosoftIdentityMismatch("malformed id_token")
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims: object = json.loads(base64.urlsafe_b64decode(segment))
    except ValueError as exc:
        raise MicrosoftIdentityMismatch("malformed id_token") from exc
    if not isinstance(claims, dict):
        raise MicrosoftIdentityMismatch("malformed id_token")
    return cast(dict[str, Any], claims)


def canonical_uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def verify_identity(
    tokens: MicrosoftTokens, config: MicrosoftAccountsSettings, *, expected_user_id: str
) -> str:
    """Return the signed-in Entra object id, or raise if it is not `expected_user_id`.

    The account must belong to the configured tenant, the token must be for
    this app, and its object id must equal the Teams person's.
    """
    if tokens.id_token is None:
        raise MicrosoftIdentityMismatch("token response carried no id_token")
    claims = id_token_claims(tokens.id_token)
    if canonical_uuid(claims.get("tid")) != config.tenant_id:
        raise MicrosoftIdentityMismatch("account belongs to another tenant")
    if claims.get("aud") != config.client_id:
        raise MicrosoftIdentityMismatch("id_token was issued to another app")
    oid = canonical_uuid(claims.get("oid"))
    if oid is None or oid != canonical_uuid(expected_user_id):
        raise MicrosoftIdentityMismatch("signed-in account is not the Teams person")
    return oid
