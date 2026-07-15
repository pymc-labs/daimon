"""Tests for the pure capability-token minter (core mint side)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from daimon.core.notebooks.capability import mint_token

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_SECRET = "primary-admin-secret"


def _decode_payload(token: str) -> dict[str, object]:
    payload_b64 = token.split(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    return json.loads(raw)


def test_mint_token_embeds_destination_and_signs_payload() -> None:
    token = mint_token(
        _SECRET, slug="my-blog", op="blog", max_bytes=1_000_000, now=_NOW, jti="abc123"
    )
    payload_b64, sig_b64 = token.split(".", 1)
    payload = _decode_payload(token)
    assert payload["slug"] == "my-blog", "slug is carried in the signed payload"
    assert payload["op"] == "blog", "op is carried in the signed payload"
    assert payload["max_bytes"] == 1_000_000, "per-upload byte budget is signed in"
    assert payload["jti"] == "abc123", "single-use id is signed in"
    assert payload["exp"] == int(_NOW.timestamp()) + 300, "default ttl is 300s past now"
    expected_sig = hmac.new(_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    got_sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    assert hmac.compare_digest(got_sig, expected_sig), "signature is HMAC-SHA256 over payload_b64"


def test_mint_token_data_op_includes_name() -> None:
    token = mint_token(
        _SECRET,
        slug="s",
        op="data",
        name="posterior.nc",
        max_bytes=2_000_000,
        now=_NOW,
        jti="j",
    )
    payload = _decode_payload(token)
    assert payload["name"] == "posterior.nc", "data tokens carry the attachment name"
    assert payload["op"] == "data", "op is data"


def test_mint_token_rejects_naive_now() -> None:
    import datetime as _dt

    naive = _dt.datetime(2026, 6, 9, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        mint_token(_SECRET, slug="s", op="blog", max_bytes=1, now=naive, jti="j")
