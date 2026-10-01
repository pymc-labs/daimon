"""Operator token scopes, mint terms and the promo issuing ceiling (pure)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from daimon.core.operator_tokens import (
    OperatorTokenError,
    issue_refusal,
    parse_operator_scopes,
    scope_tag,
    validate_operator_terms,
)


def test_parse_operator_scopes_accepts_known_scopes_and_strips_spaces() -> None:
    scopes = parse_operator_scopes([" tenant:read", "promo:create", "tenant:read"])
    assert scopes == frozenset({"tenant:read", "promo:create"}), "duplicates collapse"


def test_parse_operator_scopes_refuses_unknown_scope() -> None:
    with pytest.raises(OperatorTokenError, match="unknown scope 'admin'"):
        parse_operator_scopes(["tenant:read", "admin"])


def test_parse_operator_scopes_refuses_empty_list() -> None:
    with pytest.raises(OperatorTokenError, match="at least one scope"):
        parse_operator_scopes([])


def test_scope_tag_prefixes_the_scope() -> None:
    assert scope_tag("tenant:read") == "scope:tenant:read", "tools carry scope:<name> tags"


def test_validate_operator_terms_refuses_ceiling_without_promo_create() -> None:
    with pytest.raises(OperatorTokenError, match="promo:create"):
        validate_operator_terms(
            scopes=frozenset({"tenant:read"}), ttl_days=30, max_issued_usd=Decimal("50")
        )


@pytest.mark.parametrize("ttl_days", [0, 91])
def test_validate_operator_terms_refuses_ttl_out_of_range(ttl_days: int) -> None:
    """At most 90 days: a demoted admin's token stops at its expiry at the latest."""
    with pytest.raises(OperatorTokenError, match="ttl must be 1 to 90 days"):
        validate_operator_terms(
            scopes=frozenset({"tenant:read"}), ttl_days=ttl_days, max_issued_usd=None
        )


def test_validate_operator_terms_refuses_non_positive_ceiling() -> None:
    with pytest.raises(OperatorTokenError, match="positive"):
        validate_operator_terms(
            scopes=frozenset({"promo:create"}), ttl_days=30, max_issued_usd=Decimal("0")
        )


@pytest.mark.parametrize("ceiling", ["10.005", "0.001"])
def test_validate_operator_terms_refuses_a_ceiling_finer_than_cents(ceiling: str) -> None:
    with pytest.raises(OperatorTokenError, match="whole cents"):
        validate_operator_terms(
            scopes=frozenset({"promo:create"}), ttl_days=30, max_issued_usd=Decimal(ceiling)
        )


@pytest.mark.parametrize("ceiling", ["250", "10.50", "10.500", "1E+3"])
def test_validate_operator_terms_accepts_a_ceiling_in_cents(ceiling: str) -> None:
    validate_operator_terms(
        scopes=frozenset({"promo:create"}), ttl_days=90, max_issued_usd=Decimal(ceiling)
    )


def test_issue_refusal_allows_anything_without_a_ceiling() -> None:
    refusal = issue_refusal(
        max_issued_usd=None, issued_usd=Decimal(0), amount_usd=Decimal(500), max_redemptions=None
    )
    assert refusal is None, "a token without a ceiling issues freely"


def test_issue_refusal_requires_max_redemptions_under_a_ceiling() -> None:
    refusal = issue_refusal(
        max_issued_usd=Decimal(100),
        issued_usd=Decimal(0),
        amount_usd=Decimal(5),
        max_redemptions=None,
    )
    assert refusal is not None and "max_redemptions" in refusal, (
        "unlimited redemptions cannot be counted against a ceiling"
    )


def test_issue_refusal_counts_amount_times_redemptions_against_what_is_left() -> None:
    at_limit = issue_refusal(
        max_issued_usd=Decimal(100),
        issued_usd=Decimal(40),
        amount_usd=Decimal(20),
        max_redemptions=3,
    )
    over = issue_refusal(
        max_issued_usd=Decimal(100),
        issued_usd=Decimal(40),
        amount_usd=Decimal("20.01"),
        max_redemptions=3,
    )
    assert at_limit is None, "issuing exactly up to the ceiling is allowed"
    assert over is not None and "$60.03" in over and "$60.00" in over, (
        "the refusal names what the code could grant and what is left"
    )
