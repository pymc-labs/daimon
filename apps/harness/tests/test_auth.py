from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon_harness.auth import Authenticator
from fastapi import HTTPException


async def test_entra_validates_signature_tenant_audience_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = "11111111-1111-1111-1111-111111111111"
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.setenv("HARNESS_ENTRA_TENANT_ID", tenant)
    monkeypatch.setenv("HARNESS_ENTRA_AUDIENCE", "https://ai.azure.com")
    auth = Authenticator()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    assert auth.jwks is not None
    monkeypatch.setattr(auth.jwks, "get_signing_key_from_jwt", lambda token: public_jwk)
    now = datetime.now(UTC)
    claims = {
        "aud": "https://ai.azure.com",
        "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    await auth.verify("Bearer " + jwt.encode(claims, private_key, algorithm="RS256"))
    for overrides in [
        {"aud": "another-service"},
        {"iss": "https://login.microsoftonline.com/other/v2.0"},
        {"exp": now - timedelta(seconds=1)},
    ]:
        with pytest.raises(HTTPException) as caught:
            await auth.verify(
                "Bearer " + jwt.encode({**claims, **overrides}, private_key, algorithm="RS256")
            )
        assert caught.value.status_code == 401
    with pytest.raises(HTTPException):
        await auth.verify("Bearer unsigned-or-invalid")


def test_auth_fails_closed_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HARNESS_TOKEN", "HARNESS_ENTRA_TENANT_ID", "HARNESS_ENTRA_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="configure HARNESS_TOKEN"):
        Authenticator()


async def test_foundry_gateway_requires_platform_context_and_allowed_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_AUTH_MODE", "foundry_gateway")
    monkeypatch.setenv("HARNESS_ALLOWED_CALLERS", "host-identity")
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_AGENT_NAME", "FOUNDRY_AGENT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException):
        await Authenticator().verify(None, "host-identity", "call-1")
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_AGENT_NAME", "FOUNDRY_AGENT_SESSION_ID"):
        monkeypatch.setenv(name, "platform-value")
    auth = Authenticator()
    assert await auth.verify(None, "host-identity", "call-1") == "host-identity"
    for caller, call_id, status in [
        (None, "call-1", 401),
        ("host-identity", None, 401),
        ("other-caller", "call-1", 403),
    ]:
        with pytest.raises(HTTPException) as caught:
            await auth.verify(None, caller, call_id)
        assert caught.value.status_code == status
