"""Tests for the host-side capability-token verifier.

Mirrors the core mint side; uses the same wire format so a token minted by
daimon-core verifies here. The mint helper is inlined (the host can't import
daimon-core) to prove cross-side compatibility from the wire bytes alone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from notebook_host.capability import CapabilityClaims, verify_token

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_SECRETS = ["primary-admin-secret", "rotation-secret-2"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(secret: str, payload: dict[str, object]) -> str:
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _payload(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": "my-blog",
        "op": "blog",
        "name": None,
        "max_bytes": 1_000_000,
        "exp": int(_NOW.timestamp()) + 300,
        "jti": "j1",
    }
    base.update(over)
    return base


def test_verify_token_accepts_valid_token_and_returns_typed_claims() -> None:
    claims = verify_token(_SECRETS, _mint(_SECRETS[0], _payload()), now=_NOW)
    assert isinstance(claims, CapabilityClaims), "returns a typed claims model, not a raw dict"
    assert claims.slug == "my-blog" and claims.op == "blog", (
        "destination read off the verified payload"
    )


def test_verify_token_accepts_rotation_secret() -> None:
    claims = verify_token(_SECRETS, _mint(_SECRETS[1], _payload()), now=_NOW)
    assert claims.slug == "my-blog", "a token signed by any rotation secret verifies"


def test_verify_token_rejects_forged_signature() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, _mint("attacker-guess", _payload()), now=_NOW)
    assert exc.value.status_code == 403, "unknown signing key → 403"


def test_verify_token_rejects_tampered_slug() -> None:
    good = _mint(_SECRETS[0], _payload())
    payload_b64, sig_b64 = good.split(".", 1)
    tampered = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    tampered["slug"] = "victim-blog"
    forged = f"{_b64(json.dumps(tampered, separators=(',', ':')).encode())}.{sig_b64}"
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, forged, now=_NOW)
    assert exc.value.status_code == 403, "swapping the slug invalidates the signature"


def test_verify_token_rejects_expired_token() -> None:
    expired = _mint(_SECRETS[0], _payload(exp=int(_NOW.timestamp()) - 1))
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, expired, now=_NOW)
    assert exc.value.status_code == 403, "exp in the past → 403"


def test_verify_token_rejects_malformed_token() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, "no-dot-here", now=_NOW)
    assert exc.value.status_code == 403, "a token with no '.' separator → 403"


def test_verify_token_rejects_garbage_signature_base64() -> None:
    # has a dot, but the signature segment is not valid base64 → 403, not 500
    payload_b64 = _b64(json.dumps(_payload(), separators=(",", ":")).encode())
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, f"{payload_b64}.!!!not-base64!!!", now=_NOW)
    assert exc.value.status_code == 403, (
        "garbage base64 in the signature → 403, never an unhandled 500"
    )


def test_verify_token_rejects_validly_signed_non_claims_payload() -> None:
    # correctly signed, but the payload isn't valid claims JSON → 403 (ValidationError mapped)
    payload_b64 = _b64(b"this is not json")
    sig = hmac.new(_SECRETS[0].encode(), payload_b64.encode(), hashlib.sha256).digest()
    token = f"{payload_b64}.{_b64(sig)}"
    with pytest.raises(HTTPException) as exc:
        verify_token(_SECRETS, token, now=_NOW)
    assert exc.value.status_code == 403, "signed-but-non-claims payload → 403"
