"""OpenID Connect sign-in for the web app: authorization code with PKCE.

Discovery, the authorize URL, code exchange, refresh, token exchange for a chat sign-in
and ID token verification, all against an injected `httpx.AsyncClient`. Verification is
pure given the provider's keys and a clock reading, so the checks that matter (signature,
issuer, audience, expiry, nonce) are unit-tested with tokens signed in the test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.core.chat_accounts import CHAT_ACCOUNTS_CLAIM, parse_chat_accounts
from daimon.core.config import WebOidcSettings
from daimon.core.errors import DaimonError
from daimon.core.web_identity import VerifiedIdentity

# Asymmetric algorithms only: a symmetric one would let anyone holding the public
# client id forge a token that verifies against the shared secret.
_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
_LEEWAY = timedelta(seconds=60)
_JWKS_TTL = timedelta(hours=1)
_JWKS_MIN_REFETCH = timedelta(minutes=1)
#: RFC 8693 token exchange, with a signed JWT naming who the token is for.
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
#: A subject token is spent at once; this only bounds a replay.
SUBJECT_TOKEN_TTL = timedelta(seconds=120)


class OidcError(DaimonError):
    """The provider refused, or answered without what sign-in needs."""


class OidcTokenInvalid(OidcError):
    """The ID token failed a check; the message names the check, never the token."""


class OidcKeyUnknown(OidcTokenInvalid):
    """The token names a signing key the cached key set does not have."""


@dataclass(frozen=True, slots=True)
class OidcMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    signing_algorithms: frozenset[str]
    auth_methods: frozenset[str]


@dataclass(frozen=True, slots=True)
class OidcTokens:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int
    scope: str


def code_challenge_s256(verifier: str) -> str:
    """The PKCE challenge for a verifier (RFC 7636, S256)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload: object = response.json()
    except ValueError:
        return {}
    return cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise OidcError(f"discovery document has no {key}")
    return value


def _strings(payload: Mapping[str, Any], key: str) -> frozenset[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in cast("list[object]", value) if isinstance(item, str))


async def discover(http: httpx.AsyncClient, issuer: str) -> OidcMetadata:
    """Read the provider's discovery document and check it names the configured issuer."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    response = await http.get(url, timeout=10.0)
    if response.is_error:
        raise OidcError(f"discovery answered http {response.status_code}")
    document = _json_object(response)
    if document.get("issuer") != issuer:
        # A mismatch means tokens would be checked against some other issuer.
        raise OidcError("discovery document names a different issuer")
    return OidcMetadata(
        issuer=issuer,
        authorization_endpoint=_text(document, "authorization_endpoint"),
        token_endpoint=_text(document, "token_endpoint"),
        jwks_uri=_text(document, "jwks_uri"),
        signing_algorithms=_strings(document, "id_token_signing_alg_values_supported")
        or frozenset({"RS256"}),
        auth_methods=_strings(document, "token_endpoint_auth_methods_supported"),
    )


async def fetch_jwks(http: httpx.AsyncClient, meta: OidcMetadata) -> jwt.PyJWKSet:
    response = await http.get(meta.jwks_uri, timeout=10.0)
    if response.is_error:
        raise OidcError(f"key set answered http {response.status_code}")
    try:
        return jwt.PyJWKSet.from_dict(_json_object(response))  # pyright: ignore[reportArgumentType]  # runtime-validated JSON
    except jwt.PyJWTError as error:
        raise OidcError("key set is not usable") from error


def build_authorize_url(
    meta: OidcMetadata,
    settings: WebOidcSettings,
    *,
    state: str,
    nonce: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    params = {
        "client_id": settings.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(settings.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in meta.authorization_endpoint else "?"
    return f"{meta.authorization_endpoint}{separator}{urlencode(params)}"


async def _token_request(
    http: httpx.AsyncClient, meta: OidcMetadata, settings: WebOidcSettings, form: dict[str, str]
) -> OidcTokens:
    secret = settings.client_secret.get_secret_value()
    # The provider's advertised method wins; post is the common default.
    basic = bool(meta.auth_methods) and "client_secret_post" not in meta.auth_methods
    if basic and "client_secret_basic" in meta.auth_methods:
        response = await http.post(
            meta.token_endpoint, data=form, auth=(settings.client_id, secret), timeout=10.0
        )
    else:
        response = await http.post(
            meta.token_endpoint,
            data={**form, "client_id": settings.client_id, "client_secret": secret},
            timeout=10.0,
        )
    payload = _json_object(response)
    if response.is_error:
        # Only the error code: descriptions can echo request details.
        raise OidcError(
            str(payload.get("error") or f"token endpoint answered {response.status_code}")
        )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OidcError("token response carried no access_token")
    refresh_token = payload.get("refresh_token")
    id_token = payload.get("id_token")
    expires_in = payload.get("expires_in")
    return OidcTokens(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        expires_in=expires_in if isinstance(expires_in, int) else 0,
        scope=str(payload.get("scope") or ""),
    )


async def exchange_code(
    http: httpx.AsyncClient,
    meta: OidcMetadata,
    settings: WebOidcSettings,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> OidcTokens:
    return await _token_request(
        http,
        meta,
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    )


async def refresh(
    http: httpx.AsyncClient, meta: OidcMetadata, settings: WebOidcSettings, *, refresh_token: str
) -> OidcTokens:
    """Trade a refresh token for new tokens.

    The provider rotates refresh tokens and treats a second use of the old one as
    replay, so callers serialize refreshes per person.
    """
    return await _token_request(
        http, meta, settings, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def sign_subject_token(
    *,
    key: Ed25519PrivateKey,
    issuer: str,
    audience: str,
    subject: str,
    workspace: str,
    now: datetime,
    jti: str,
) -> str:
    """The JWT a token exchange presents: who (`sub`) in which workspace (`ws`), each as
    `<platform>:<id>`, signed EdDSA like a caller identity."""
    issued = int(now.timestamp())
    claims = {
        "iss": issuer,
        "aud": audience,
        "iat": issued,
        "exp": issued + int(SUBJECT_TOKEN_TTL.total_seconds()),
        "jti": jti,
        "sub": subject,
        "ws": workspace,
    }
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"typ": "JWT"})


async def exchange_subject_token(
    http: httpx.AsyncClient, meta: OidcMetadata, settings: WebOidcSettings, *, subject_token: str
) -> OidcTokens:
    """Trade a signed subject token for an access token, as the web client."""
    return await _token_request(
        http,
        meta,
        settings,
        {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": JWT_TOKEN_TYPE,
            "scope": " ".join(settings.scopes),
        },
    )


def _signing_key(jwks: jwt.PyJWKSet, kid: str | None) -> jwt.PyJWK:
    keys = list(jwks.keys)
    if kid is None:
        if len(keys) == 1:
            return keys[0]
        raise OidcTokenInvalid("token names no signing key")
    for key in keys:
        if key.key_id == kid:
            return key
    raise OidcKeyUnknown("token names an unknown signing key")


def _is_true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _claim_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in cast("list[object]", value) if isinstance(item, str))
    return ()


def _epoch(claims: Mapping[str, Any], name: str) -> datetime | None:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def verify_id_token(
    tokens: OidcTokens,
    meta: OidcMetadata,
    jwks: jwt.PyJWKSet,
    settings: WebOidcSettings,
    *,
    nonce: str,
    now: datetime,
) -> VerifiedIdentity:
    """Check the ID token and read who signed in.

    Signature (asymmetric algorithms the provider advertises), issuer, audience,
    expiry, not-before and nonce are all required to pass. The chat accounts it vouches
    for are read only where the settings trust them; malformed entries are dropped.
    """
    if tokens.id_token is None:
        raise OidcTokenInvalid("token response carried no id_token")
    try:
        header = jwt.get_unverified_header(tokens.id_token)
        algorithm = header.get("alg")
        if algorithm not in _ALGORITHMS or algorithm not in meta.signing_algorithms:
            raise OidcTokenInvalid("token is signed with an algorithm that is not allowed")
        key = _signing_key(jwks, header.get("kid"))
        # Expiry is checked below against `now`, so the clock is the caller's.
        claims = jwt.decode(
            tokens.id_token,
            key=key.key,
            algorithms=[algorithm],
            audience=settings.client_id,
            issuer=meta.issuer,
            options={
                "require": ["exp", "iss", "aud", "sub"],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as error:
        raise OidcTokenInvalid(f"id token rejected: {type(error).__name__}") from error
    expires = _epoch(claims, "exp")
    if expires is None or now > expires + _LEEWAY:
        raise OidcTokenInvalid("id token has expired")
    not_before = _epoch(claims, "nbf")
    if not_before is not None and now + _LEEWAY < not_before:
        raise OidcTokenInvalid("id token is not valid yet")
    audience = _claim_strings(claims.get("aud"))
    if len(audience) > 1 and claims.get("azp") != settings.client_id:
        raise OidcTokenInvalid("id token was issued to a different party")
    claimed_nonce = claims.get("nonce")
    if not isinstance(claimed_nonce, str) or not hmac.compare_digest(
        claimed_nonce.encode(), nonce.encode()
    ):
        raise OidcTokenInvalid("id token nonce does not match")
    subject = claims["sub"]
    if not isinstance(subject, str) or not subject:
        raise OidcTokenInvalid("id token has no subject")
    email = claims.get("email")
    email_text = email if isinstance(email, str) and email else None
    display_name = next(
        (
            value
            for value in (
                claims.get("name"),
                claims.get("preferred_username"),
                email_text.split("@")[0] if email_text else None,
            )
            if isinstance(value, str) and value.strip()
        ),
        "Signed-in person",
    )
    return VerifiedIdentity(
        provider="oidc",
        issuer=meta.issuer,
        subject=subject,
        display_name=display_name.strip(),
        email=email_text,
        email_verified=_is_true(claims.get("email_verified")),
        groups=_claim_strings(claims.get(settings.groups_claim)),
        chat_accounts=(
            parse_chat_accounts(_claim_strings(claims.get(CHAT_ACCOUNTS_CLAIM)))
            if settings.trust_chat_accounts
            else ()
        ),
    )


class OidcCache:
    """The provider's discovery document and signing keys, fetched lazily and reused.

    Metadata is kept for the process's life; keys for an hour, and refetched once (at
    most a minute apart) when a token names a key the cache lacks, which is how a
    provider's key rotation shows up.
    """

    def __init__(self, http: httpx.AsyncClient, settings: WebOidcSettings) -> None:
        self._http = http
        self._settings = settings
        self._meta: OidcMetadata | None = None
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at: datetime | None = None

    async def metadata(self) -> OidcMetadata:
        if self._meta is None:
            self._meta = await discover(self._http, self._settings.issuer)
        return self._meta

    async def _keys(self, now: datetime, *, force: bool) -> jwt.PyJWKSet:
        stale = self._jwks_at is None or now - self._jwks_at >= _JWKS_TTL
        recent = self._jwks_at is not None and now - self._jwks_at < _JWKS_MIN_REFETCH
        if self._jwks is None or stale or (force and not recent):
            self._jwks = await fetch_jwks(self._http, await self.metadata())
            self._jwks_at = now
        return self._jwks

    async def verify(self, tokens: OidcTokens, *, nonce: str, now: datetime) -> VerifiedIdentity:
        meta = await self.metadata()
        try:
            return verify_id_token(
                tokens,
                meta,
                await self._keys(now, force=False),
                self._settings,
                nonce=nonce,
                now=now,
            )
        except OidcKeyUnknown:
            return verify_id_token(
                tokens,
                meta,
                await self._keys(now, force=True),
                self._settings,
                nonce=nonce,
                now=now,
            )
