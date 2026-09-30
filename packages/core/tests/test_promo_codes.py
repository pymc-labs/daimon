"""Pure promo code rules: normalization, generation, term validation, redeemability."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

import pytest
from daimon.core.promo_codes import (
    PROMO_CODE_ALPHABET,
    PromoCodeError,
    PromoRefusal,
    build_promo_code_terms,
    describe_refusal,
    generate_promo_code,
    hash_promo_code,
    is_granted_on_redeem,
    is_well_formed_promo_code,
    normalize_promo_code,
    parse_utc_timestamp,
    redeem_refusal,
)
from daimon.core.stores.domain import PromoCodeRow

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _code(**overrides: Any) -> PromoCodeRow:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "note": None,
        "amount_usd": Decimal("10"),
        "kind": "credit",
        "credit_starts_at": None,
        "credit_ends_at": None,
        "redeem_starts_at": None,
        "redeem_ends_at": None,
        "max_redemptions": None,
        "redeemed_count": 0,
        "created_at": NOW - HOUR,
        "revoked_at": None,
    }
    return PromoCodeRow(**(fields | overrides))


def test_normalize_ignores_case_whitespace_and_dashes() -> None:
    assert normalize_promo_code("  ab3de-fg h7k\n") == "AB3DEFGH7K"
    assert hash_promo_code(normalize_promo_code("abc-def")) == hash_promo_code("ABCDEF")


@pytest.mark.parametrize("raw", ["", "ABC", "AB$DEF", "ÄBCDEF", "A" * 65])
def test_malformed_codes_are_rejected(raw: str) -> None:
    assert not is_well_formed_promo_code(normalize_promo_code(raw))


def test_generated_codes_are_grouped_unambiguous_and_redeemable() -> None:
    code = generate_promo_code()
    assert re.fullmatch(r"[0-9A-Z]{5}(-[0-9A-Z]{5}){3}", code), code
    assert set(code.replace("-", "")) <= set(PROMO_CODE_ALPHABET)
    assert not set("ILOU") & set(code)
    assert is_well_formed_promo_code(normalize_promo_code(code.lower()))
    assert generate_promo_code() != code


def test_generation_maps_bytes_onto_the_alphabet() -> None:
    code = generate_promo_code(random_bytes=lambda n: bytes(range(n)))
    assert code == "01234-56789-ABCDE-FGHJK"


def test_parse_utc_timestamp_reads_naive_as_utc_and_converts_offsets() -> None:
    assert parse_utc_timestamp("2026-05-01T12:00") == NOW
    assert parse_utc_timestamp("2026-05-01T14:00+02:00") == NOW
    with pytest.raises(PromoCodeError):
        parse_utc_timestamp("next tuesday")


def test_credit_terms() -> None:
    terms = build_promo_code_terms(
        amount_usd=Decimal("25.50"), timed=False, max_redemptions=3, note="  launch  "
    )
    assert (terms.kind, terms.amount_usd, terms.note) == ("credit", Decimal("25.50"), "launch")
    assert terms.credit_starts_at is None and terms.redeem_ends_at is None


def test_timed_terms_stop_redemption_when_the_credit_ends() -> None:
    terms = build_promo_code_terms(
        amount_usd=Decimal("5"), timed=True, credit_starts_at=NOW, credit_ends_at=NOW + HOUR
    )
    assert terms.kind == "timed"
    assert terms.redeem_ends_at == NOW + HOUR


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"amount_usd": Decimal("0"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("-1"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("NaN"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("1.005"), "timed": False}, "two decimal"),
        ({"amount_usd": Decimal("1"), "timed": True, "credit_ends_at": NOW}, "both"),
        (
            {
                "amount_usd": Decimal("1"),
                "timed": True,
                "credit_starts_at": NOW,
                "credit_ends_at": NOW,
            },
            "before the credit end",
        ),
        (
            {
                "amount_usd": Decimal("1"),
                "timed": True,
                "credit_starts_at": NOW,
                "credit_ends_at": NOW + HOUR,
                "redeem_ends_at": NOW + 2 * HOUR,
            },
            "after its credit ends",
        ),
        ({"amount_usd": Decimal("1"), "timed": False, "credit_starts_at": NOW}, "timed code"),
        (
            {
                "amount_usd": Decimal("1"),
                "timed": False,
                "redeem_starts_at": NOW,
                "redeem_ends_at": NOW,
            },
            "redemption start",
        ),
        ({"amount_usd": Decimal("1"), "timed": False, "max_redemptions": 0}, "at least 1"),
    ],
)
def test_inconsistent_terms_are_refused(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(PromoCodeError, match=message):
        build_promo_code_terms(**kwargs)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"revoked_at": NOW - HOUR}, "revoked"),
        ({"redeem_starts_at": NOW + HOUR}, "not_started"),
        ({"redeem_ends_at": NOW}, "expired"),
        ({"kind": "timed", "credit_starts_at": NOW - 2 * HOUR, "credit_ends_at": NOW}, "expired"),
        ({"max_redemptions": 2, "redeemed_count": 2}, "exhausted"),
        ({"max_redemptions": 2, "redeemed_count": 1}, None),
    ],
)
def test_redeem_refusal(overrides: dict[str, Any], reason: str | None) -> None:
    assert redeem_refusal(_code(**overrides), now=NOW) == reason


def test_timed_credit_is_granted_on_redeem_only_once_started() -> None:
    assert is_granted_on_redeem(_code(), now=NOW)
    early = _code(kind="timed", credit_starts_at=NOW + HOUR, credit_ends_at=NOW + 2 * HOUR)
    assert not is_granted_on_redeem(early, now=NOW)
    assert is_granted_on_redeem(early, now=NOW + HOUR)


def test_every_refusal_has_a_sentence() -> None:
    for reason in get_args(PromoRefusal):
        assert describe_refusal(reason).endswith(".")
