"""Tests for the pure bundle-handle mint/verify pair."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.bundle_handle import mint, verify


def test_round_trip_returns_claims_equal_to_what_was_minted() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc123",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="a" * 64,
        now=now,
        ttl_days=90,
    )
    claims = verify("secret", handle, tenant_id=tenant_id, agent_id=agent_id, now=now)
    assert claims is not None, "a freshly minted handle should verify"
    assert claims.file_id == "file_abc123", "file_id should round-trip"
    assert claims.tenant_id == tenant_id, "tenant_id should round-trip"
    assert claims.agent_id == agent_id, "agent_id should round-trip"
    assert claims.sha256 == "a" * 64, "sha256 should round-trip"
    assert claims.exp == int((now + timedelta(days=90)).timestamp()), "exp should be now + ttl_days"


def test_verify_returns_none_when_signed_with_a_different_secret() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret-one",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="b" * 64,
        now=now,
        ttl_days=90,
    )
    claims = verify("secret-two", handle, tenant_id=tenant_id, agent_id=agent_id, now=now)
    assert claims is None, "a handle signed with a different secret must not verify"


def test_verify_returns_none_when_payload_half_is_tampered() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="c" * 64,
        now=now,
        ttl_days=90,
    )
    payload_b64, sig_b64 = handle.split(".", 1)
    flipped_char = "A" if payload_b64[0] != "A" else "B"
    tampered = flipped_char + payload_b64[1:] + "." + sig_b64
    claims = verify("secret", tampered, tenant_id=tenant_id, agent_id=agent_id, now=now)
    assert claims is None, "flipping a character of the payload half must not verify"


def test_verify_returns_none_when_signature_half_is_tampered() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="d" * 64,
        now=now,
        ttl_days=90,
    )
    payload_b64, sig_b64 = handle.split(".", 1)
    flipped_char = "A" if sig_b64[0] != "A" else "B"
    tampered = payload_b64 + "." + flipped_char + sig_b64[1:]
    claims = verify("secret", tampered, tenant_id=tenant_id, agent_id=agent_id, now=now)
    assert claims is None, "flipping a character of the signature half must not verify"


def test_verify_returns_none_when_exp_has_passed() -> None:
    mint_time = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="e" * 64,
        now=mint_time,
        ttl_days=1,
    )
    later = mint_time + timedelta(days=2)
    claims = verify("secret", handle, tenant_id=tenant_id, agent_id=agent_id, now=later)
    assert claims is None, "a handle whose exp has passed must not verify"


def test_verify_returns_none_when_checked_against_the_wrong_tenant() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    agent_id = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_a,
        agent_id=agent_id,
        sha256="f" * 64,
        now=now,
        ttl_days=90,
    )
    claims = verify("secret", handle, tenant_id=tenant_b, agent_id=agent_id, now=now)
    assert claims is None, "a handle minted for tenant A must not verify against tenant B"


def test_verify_returns_none_when_checked_against_the_wrong_agent() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_a,
        sha256="0" * 64,
        now=now,
        ttl_days=90,
    )
    claims = verify("secret", handle, tenant_id=tenant_id, agent_id=agent_b, now=now)
    assert claims is None, "a handle minted for agent A must not verify against agent B"


def test_verify_returns_none_when_handle_has_no_dot() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    claims = verify("secret", "no-dot-here", tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), now=now)
    assert claims is None, "a handle with no '.' separator must not verify"


def test_verify_returns_none_when_handle_has_two_dots() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    claims = verify("secret", "a.b.c", tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), now=now)
    assert claims is None, "a handle with two '.' separators must not verify"


def test_verify_returns_none_when_payload_has_four_fields() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    # Build a handle whose payload is missing the sha256 field (4 parts, not 5).
    handle = mint(
        "secret",
        file_id="file_abc",
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="1" * 64,
        now=now,
        ttl_days=90,
    )
    payload_b64, _ = handle.split(".", 1)
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode()
    fields = raw.split("|")
    four_field_payload = "|".join(fields[:4])
    four_field_b64 = base64.urlsafe_b64encode(four_field_payload.encode()).decode().rstrip("=")
    # Re-sign so this exercises the field-count check, not the signature check.
    import hashlib
    import hmac

    sig = hmac.new(b"secret", four_field_b64.encode(), hashlib.sha256).digest()
    tampered_sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    tampered = f"{four_field_b64}.{tampered_sig_b64}"
    claims = verify("secret", tampered, tenant_id=tenant_id, agent_id=agent_id, now=now)
    assert claims is None, "a payload with four fields (missing one) must not verify"


def test_mint_rejects_naive_now() -> None:
    naive = datetime(2026, 6, 9, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        mint(
            "secret",
            file_id="file_abc",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            sha256="2" * 64,
            now=naive,
            ttl_days=90,
        )


def test_mint_rejects_file_id_containing_pipe() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"must not contain '\|'"):
        mint(
            "secret",
            file_id="file|abc",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            sha256="3" * 64,
            now=now,
            ttl_days=90,
        )
