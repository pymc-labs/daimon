import base64
import hashlib
import hmac
import json
import time

from daimon.core.slack_file_token import (
    SlackFileRef,
    mint_file_token,
    verify_file_token,
)

SECRET = "test-secret-value"


def test_verify_returns_ref_when_token_valid_and_unexpired():
    now = int(time.time())
    token = mint_file_token(team_id="T1", file_id="F1", exp=now + 100, secret=SECRET)
    ref = verify_file_token(token, secret=SECRET, now=now)
    assert ref == SlackFileRef(team_id="T1", file_id="F1"), "valid token round-trips to its ref"


def test_verify_returns_none_when_expired():
    now = int(time.time())
    token = mint_file_token(team_id="T1", file_id="F1", exp=now - 1, secret=SECRET)
    assert verify_file_token(token, secret=SECRET, now=now) is None, "expired token is rejected"


def test_verify_returns_none_when_signature_forged():
    now = int(time.time())
    token = mint_file_token(team_id="T1", file_id="F1", exp=now + 100, secret=SECRET)
    assert verify_file_token(token, secret="wrong-secret", now=now) is None, (
        "token signed with a different secret is rejected"
    )


def test_verify_returns_none_when_structurally_malformed():
    assert verify_file_token("not-a-token", secret=SECRET, now=0) is None, (
        "token without the '.' separator is rejected, not raised"
    )
    assert verify_file_token("!!!.$$$", secret=SECRET, now=0) is None, (
        "non-base64 halves are rejected, not raised"
    )


def _sign_raw(payload_bytes: bytes, secret: str) -> str:
    """Helper to mint a token with a given payload (for testing malformed payloads)."""
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload_bytes).decode()
        + "."
        + base64.urlsafe_b64encode(sig).decode()
    )


def test_verify_returns_none_for_validly_signed_but_malformed_payloads():
    cases = [
        b"not json at all",
        json.dumps([1, 2, 3]).encode(),  # JSON array, not object
        json.dumps({"team_id": "T1", "file_id": "F1"}).encode(),  # missing exp
        json.dumps({"team_id": "T1", "file_id": "F1", "exp": "soon"}).encode(),  # non-numeric exp
    ]
    for payload in cases:
        token = _sign_raw(payload, SECRET)
        assert verify_file_token(token, secret=SECRET, now=0) is None, (
            f"validly-signed but malformed payload must verify to None: {payload!r}"
        )
