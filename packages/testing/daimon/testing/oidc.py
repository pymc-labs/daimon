"""An in-process OpenID Connect provider for sign-in tests.

`FakeOidcProvider.handler` answers discovery, the key set and the token endpoint for an
`httpx.MockTransport`. Codes are issued by the test (standing in for the browser's
visit to the authorize endpoint) and redeemed only with the PKCE verifier that matches
the challenge they were issued against, as a real provider would.
"""

from __future__ import annotations

import functools
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.web_oidc import code_challenge_s256
from jwt.algorithms import RSAAlgorithm


@functools.cache
def _signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass(frozen=True, slots=True)
class _IssuedCode:
    challenge: str
    id_token: str
    refresh_token: str | None


class FakeOidcProvider:
    """Signs ID tokens and plays the discovery, key set and token endpoints."""

    def __init__(
        self, *, issuer: str = "https://id.example.com", client_id: str = "web-client"
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.kid = "key-1"
        self.token_requests: list[dict[str, str]] = []
        self._codes: dict[str, _IssuedCode] = {}
        # Live refresh tokens: each works once and is replaced by a new one, as with rotation.
        self._refresh: set[str] = set()

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(_signing_key().public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def sign(
        self, claims: dict[str, Any], *, kid: str | None = None, algorithm: str = "RS256"
    ) -> str:
        """Sign arbitrary claims, by default with the key the key set publishes."""
        key: Any = (
            _signing_key() if algorithm.startswith("RS") else "shared-secret-shared-secret-1234"
        )
        return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid or self.kid})

    def id_token(
        self,
        *,
        nonce: str,
        subject: str = "user-1",
        email: str | None = "ada@example.com",
        email_verified: bool = True,
        name: str | None = "Ada Lovelace",
        groups: list[str] | None = None,
        now: float | None = None,
        lifetime_s: int = 600,
        audience: str | None = None,
        issuer: str | None = None,
    ) -> str:
        issued = int(now if now is not None else time.time())
        claims: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "aud": audience or self.client_id,
            "sub": subject,
            "iat": issued,
            "exp": issued + lifetime_s,
            "nonce": nonce,
            "email_verified": email_verified,
        }
        if email is not None:
            claims["email"] = email
        if name is not None:
            claims["name"] = name
        if groups is not None:
            claims["groups"] = groups
        return self.sign(claims)

    def issue_code(
        self, *, code_challenge: str, id_token: str, refresh_token: str | None = None
    ) -> str:
        """What the provider hands the browser after the person signs in."""
        code = secrets.token_urlsafe(16)
        self._codes[code] = _IssuedCode(code_challenge, id_token, refresh_token)
        if refresh_token is not None:
            self._refresh.add(refresh_token)
        return code

    def allow_refresh(self, refresh_token: str) -> None:
        """Accept `refresh_token` once, as if an earlier sign-in had issued it."""
        self._refresh.add(refresh_token)

    def _refreshed(self, form: dict[str, str]) -> httpx.Response:
        old = form.get("refresh_token", "")
        if old not in self._refresh:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self._refresh.discard(old)
        rotated = "refresh-" + secrets.token_urlsafe(8)
        self._refresh.add(rotated)
        return httpx.Response(
            200,
            json={
                "access_token": "access-" + secrets.token_urlsafe(8),
                "token_type": "Bearer",
                "expires_in": 600,
                "refresh_token": rotated,
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token",
                    "jwks_uri": f"{self.issuer}/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if form.get("grant_type") == "refresh_token":
                return self._refreshed(form)
            issued = self._codes.pop(form.get("code", ""), None)
            if (
                issued is None
                or form.get("grant_type") != "authorization_code"
                or code_challenge_s256(form.get("code_verifier", "")) != issued.challenge
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            body: dict[str, Any] = {
                "access_token": "access-" + secrets.token_urlsafe(8),
                "token_type": "Bearer",
                "expires_in": 600,
                "id_token": issued.id_token,
            }
            if issued.refresh_token is not None:
                body["refresh_token"] = issued.refresh_token
            return httpx.Response(200, json=body)
        return httpx.Response(404)
