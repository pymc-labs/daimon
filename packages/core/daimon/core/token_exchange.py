"""RFC 8693 token exchange: trade a signed assertion of who a person is for their access
token at a server that trusts this deployment's signing key.

The subject token is an EdDSA JWT naming the person's chat account (`sub`) and its
workspace (`ws`), signed with the caller-identity key. Signing is pure; the exchange is
one POST against an injected `httpx.AsyncClient`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.core.errors import DaimonError

TOKEN_EXCHANGE_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:jwt"
#: A subject token is spent at once; this only bounds a replay.
SUBJECT_TOKEN_TTL: Final = timedelta(seconds=120)


class TokenExchangeError(DaimonError):
    """The server refused the exchange, or answered without a token."""


@dataclass(frozen=True, slots=True)
class ExchangedToken:
    access_token: str
    # 0 when the server did not say.
    expires_in: int


@dataclass(frozen=True, slots=True)
class ExchangeClient:
    """Where to exchange and as which OAuth client."""

    token_url: str
    client_id: str
    client_secret: str
    scope: str


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
    """The JWT an exchange presents: who (`sub`) in which workspace (`ws`), each as
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
    http: httpx.AsyncClient, client: ExchangeClient, *, subject_token: str, timeout: float
) -> ExchangedToken:
    """Trade a signed subject token for an access token."""
    response = await http.post(
        client.token_url,
        data={
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": JWT_TOKEN_TYPE,
            "scope": client.scope,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        },
        timeout=timeout,
    )
    try:
        payload: object = response.json()
    except ValueError:
        payload = None
    body = cast("dict[str, object]", payload) if isinstance(payload, dict) else {}
    if response.is_error:
        # Only the error code: descriptions can echo request details.
        raise TokenExchangeError(
            str(body.get("error") or f"token endpoint answered {response.status_code}")
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenExchangeError("token response carried no access_token")
    expires_in = body.get("expires_in")
    return ExchangedToken(
        access_token=access_token,
        expires_in=expires_in if isinstance(expires_in, int) else 0,
    )
