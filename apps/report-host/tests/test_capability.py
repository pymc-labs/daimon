"""Tests for the host-side capability-token verifier — never imports daimon.

Mirrors the core mint side; uses the same wire format so a token minted by
daimon-core verifies here. The mint helper is inlined here (report-host
cannot import daimon-core) to prove cross-side compatibility from the wire
bytes alone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from pydantic import SecretStr
from report_host.capability import CapabilityClaims, verify_token

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_SECRETS = [SecretStr("primary-admin-secret"), SecretStr("rotation-secret-2")]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(secret: str, payload: dict[str, object]) -> str:
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _payload(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": "my-report",
        "op": "report",
        "name": None,
        "max_bytes": 1_000_000,
        "exp": int(_NOW.timestamp()) + 300,
        "jti": "j1",
    }
    base.update(over)
    return base


def test_verify_token_accepts_valid_token_and_returns_typed_claims() -> None:
    claims = verify_token(_SECRETS, _mint(_SECRETS[0].get_secret_value(), _payload()), now=_NOW)
    assert isinstance(claims, CapabilityClaims), "returns a typed claims model, not a raw dict"
    assert claims.slug == "my-report" and claims.op == "report", (
        "destination read off the verified payload"
    )


def test_verify_token_accepts_second_of_two_configured_secrets() -> None:
    claims = verify_token(_SECRETS, _mint(_SECRETS[1].get_secret_value(), _payload()), now=_NOW)
    assert claims is not None, "a token signed by any configured secret verifies"
    assert claims.slug == "my-report"


def test_verify_token_rejects_forged_signature() -> None:
    token = _mint("attacker-guess", _payload())
    assert verify_token(_SECRETS, token, now=_NOW) is None, "unknown signing key must fail closed"


def test_verify_token_rejects_tampered_payload() -> None:
    good = _mint(_SECRETS[0].get_secret_value(), _payload())
    payload_b64, sig_b64 = good.split(".", 1)
    tampered = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    tampered["slug"] = "victim-report"
    forged = f"{_b64(json.dumps(tampered, separators=(',', ':')).encode())}.{sig_b64}"
    assert verify_token(_SECRETS, forged, now=_NOW) is None, (
        "swapping the slug invalidates the signature"
    )


def test_verify_token_rejects_tampered_signature() -> None:
    good = _mint(_SECRETS[0].get_secret_value(), _payload())
    payload_b64, _sig_b64 = good.split(".", 1)
    corrupted_sig = _b64(b"not-the-real-signature-bytes")
    forged = f"{payload_b64}.{corrupted_sig}"
    assert verify_token(_SECRETS, forged, now=_NOW) is None, "a corrupted signature must fail"


def test_verify_token_rejects_expired_token() -> None:
    expired = _mint(_SECRETS[0].get_secret_value(), _payload(exp=int(_NOW.timestamp()) - 1))
    assert verify_token(_SECRETS, expired, now=_NOW) is None, "exp in the past must fail"


def test_verify_token_rejects_secret_not_in_list() -> None:
    token = _mint("not-a-configured-secret", _payload())
    assert verify_token(_SECRETS, token, now=_NOW) is None


def test_verify_token_rejects_blog_op() -> None:
    token = _mint(_SECRETS[0].get_secret_value(), _payload(op="blog"))
    assert verify_token(_SECRETS, token, now=_NOW) is None, (
        "report-host accepts exactly the report operation"
    )


def test_verify_token_rejects_malformed_token() -> None:
    assert verify_token(_SECRETS, "no-dot-here", now=_NOW) is None


def test_verify_token_rejects_garbage_signature_base64() -> None:
    payload_b64 = _b64(json.dumps(_payload(), separators=(",", ":")).encode())
    assert verify_token(_SECRETS, f"{payload_b64}.!!!not-base64!!!", now=_NOW) is None
