"""Drift gate between the mint and verify halves of the capability-token pair.

The mint side lives in ``daimon.core.notebooks.capability``; the verify side
is duplicated (not imported) into each standalone app, including
``report_host.capability``. This is the one place a test is allowed to
import both — because a drift between them would originate here, on the
mint side, and should be caught before it ships.

What breaks this test: any change to the payload key set, or the signing
input (which bytes get HMAC'd, or with what algorithm). The key-set case is
only a real drift gate because ``report_host.capability.CapabilityClaims``
sets ``extra="forbid"`` — an unrecognized field in the signed payload is a
hard validation failure on the verify side, not a silently-ignored extra.
Confirmed by mutation: temporarily adding a key to the core mint payload
turns this test red; reverted after confirming.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.notebooks.capability import mint_token
from pydantic import SecretStr
from report_host.capability import verify_token

_SECRET = "shared-report-secret"
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def test_token_minted_by_core_verifies_in_report_host_and_claims_match() -> None:
    token = mint_token(
        _SECRET,
        slug="q3-financials",
        op="report",
        max_bytes=25 * 1024 * 1024,
        now=_NOW,
        jti="jti-123",
        ttl_seconds=300,
        name="bundle.tar.gz",
    )

    claims = verify_token([SecretStr(_SECRET)], token, now=_NOW)

    assert claims is not None, "a token minted by core must verify in report_host"
    assert claims.slug == "q3-financials"
    assert claims.op == "report"
    assert claims.name == "bundle.tar.gz"
    assert claims.max_bytes == 25 * 1024 * 1024
    assert claims.jti == "jti-123"
    assert claims.exp == int((_NOW + timedelta(seconds=300)).timestamp())


def test_token_minted_by_core_is_rejected_after_expiry_in_report_host() -> None:
    token = mint_token(
        _SECRET,
        slug="q3-financials",
        op="report",
        max_bytes=1_000,
        now=_NOW,
        jti="jti-456",
        ttl_seconds=1,
    )

    claims = verify_token([SecretStr(_SECRET)], token, now=_NOW + timedelta(seconds=2))

    assert claims is None, "an expired core-minted token must be refused by report_host"
