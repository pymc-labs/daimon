"""Tests for github_app_auth: RS256 JWT minting, webhook-signature verification,
and installation-token exchange.

All pure functions are tested without I/O. The token-exchange shell function uses
httpx.MockTransport (transport-level fake — guideline:testing).
"""

from __future__ import annotations

import hashlib
import hmac

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_installation_id_for_repo,
    mint_installation_token,
    verify_signature,
)

# ---------------------------------------------------------------------------
# RSA key pair helper (inline — each test owns its key material)
# ---------------------------------------------------------------------------


def _generate_rsa_keypair() -> tuple[str, bytes]:
    """Return (private_key_pem_str, public_key_pem_bytes) for RS256 tests."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Task 1: build_app_jwt
# ---------------------------------------------------------------------------


def test_build_app_jwt_claims() -> None:
    """build_app_jwt returns a token with iss=app_id, iat=now-60, exp=now+540."""
    private_pem, public_pem = _generate_rsa_keypair()
    now = 1_000_000

    token = build_app_jwt(private_pem, "12345", now=now)

    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345", "iss must equal the app_id"
    assert claims["iat"] == now - 60, "iat must be now-60 (clock drift)"
    assert claims["exp"] == now + 9 * 60, "exp must be now+540 (9 minutes)"


# ---------------------------------------------------------------------------
# Task 1: verify_signature
# ---------------------------------------------------------------------------


def test_verify_signature_accepts_valid() -> None:
    """A correctly-signed body is accepted."""
    secret = "my-webhook-secret"
    body = b'{"action": "push"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"

    result = verify_signature(secret, body, header)

    assert result is True, "valid HMAC-SHA256 signature must be accepted"


def test_verify_signature_rejects_forged() -> None:
    """A tampered body (wrong secret) is rejected."""
    secret = "my-webhook-secret"
    body = b'{"action": "push"}'
    digest = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"

    result = verify_signature(secret, body, header)

    assert result is False, "signature computed with wrong secret must be rejected"


def test_verify_signature_rejects_tampered_body() -> None:
    """A signature valid for original body is rejected if body is tampered."""
    secret = "my-webhook-secret"
    original_body = b'{"action": "push"}'
    tampered_body = b'{"action": "push", "tampered": true}'
    digest = hmac.new(secret.encode(), original_body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"

    result = verify_signature(secret, tampered_body, header)

    assert result is False, "signature for original body must be rejected on tampered body"


def test_verify_signature_rejects_missing_prefix() -> None:
    """A header not starting with 'sha256=' is rejected."""
    secret = "my-webhook-secret"
    body = b'{"action": "push"}'
    # Valid SHA-256 but missing the 'sha256=' prefix
    raw_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    result = verify_signature(secret, body, raw_hex)

    assert result is False, "header without 'sha256=' prefix must be rejected"


# ---------------------------------------------------------------------------
# Task 2: mint_installation_token (transport-level mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_installation_token_request_shape() -> None:
    """mint_installation_token POSTs the correct URL, JWT header, Accept, and API version."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            status_code=201,
            json={"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    token = await mint_installation_token(client, jwt="test-jwt", installation_id=42)

    assert len(captured) == 1, "exactly one POST must be issued"
    req = captured[0]
    assert req.method == "POST", "must use POST"
    assert str(req.url) == "https://api.github.com/app/installations/42/access_tokens", (
        "URL must target the correct GitHub endpoint for installation 42"
    )
    assert req.headers["Authorization"] == "Bearer test-jwt", (
        "Authorization header must carry the JWT as a Bearer token"
    )
    assert req.headers["Accept"] == "application/vnd.github+json", (
        "Accept header must be the GitHub JSON media type"
    )
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28", (
        "API version header must be pinned to 2022-11-28"
    )
    assert token == "ghs_x", "returned token must be the value from JSON response"


@pytest.mark.asyncio
async def test_mint_installation_token_raises_on_401() -> None:
    """mint_installation_token raises (does not swallow) on a 401 response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    with pytest.raises(httpx.HTTPStatusError):
        await mint_installation_token(client, jwt="bad-jwt", installation_id=99)


# ---------------------------------------------------------------------------
# Task 1 (97-01): get_installation_id_for_repo (transport-level mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_installation_id_for_repo_request_shape_and_id() -> None:
    """get_installation_id_for_repo GETs the correct URL with the App JWT
    header and returns the installation id from a 200 response."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code=200, json={"id": 4242, "account": {"login": "acme"}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    installation_id = await get_installation_id_for_repo(
        client, jwt="test-jwt", owner="acme", repo="widgets"
    )

    assert len(captured) == 1, "exactly one GET must be issued"
    req = captured[0]
    assert req.method == "GET", "must use GET"
    assert str(req.url) == "https://api.github.com/repos/acme/widgets/installation", (
        "URL must target the installation-lookup endpoint for owner/repo"
    )
    assert req.headers["Authorization"] == "Bearer test-jwt", (
        "Authorization header must carry the App JWT as a Bearer token (not an installation token)"
    )
    assert req.headers["Accept"] == "application/vnd.github+json", (
        "Accept header must be the GitHub JSON media type"
    )
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28", (
        "API version header must be pinned to 2022-11-28"
    )
    assert installation_id == 4242, "returned id must be the value from the JSON response"


@pytest.mark.asyncio
async def test_get_installation_id_for_repo_returns_none_on_404() -> None:
    """A 404 (App not installed on the repo) returns None, not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    installation_id = await get_installation_id_for_repo(
        client, jwt="test-jwt", owner="acme", repo="private-repo"
    )

    assert installation_id is None, "404 must be treated as 'App not installed', not an error"


@pytest.mark.asyncio
async def test_get_installation_id_for_repo_raises_on_500() -> None:
    """A non-404 non-2xx response raises (does not swallow the real error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    with pytest.raises(httpx.HTTPStatusError):
        await get_installation_id_for_repo(client, jwt="test-jwt", owner="acme", repo="widgets")
