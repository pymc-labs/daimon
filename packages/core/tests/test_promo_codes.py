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
    normalize_chosen_promo_code,
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
    """Normalization drops case, whitespace and dashes before hashing."""
    assert normalize_promo_code("  ab3de-fg h7k\n") == "AB3DEFGH7K", "case and separators drop"
    assert hash_promo_code(normalize_promo_code("abc-def")) == hash_promo_code("ABCDEF"), (
        "the hash should match however the code is typed"
    )


def test_normalize_folds_crockford_look_alikes() -> None:
    """Look-alike letters normalize to the digits they resemble."""
    assert normalize_promo_code("o1l-iO0") == "011100", "O reads as 0, I and L as 1"
    generated = generate_promo_code()
    misread = generated.replace("0", "O").replace("1", "l")
    assert normalize_promo_code(misread) == normalize_promo_code(generated), (
        "a generated code misread with look-alikes should still match"
    )


@pytest.mark.parametrize("raw", ["", "ABC", "AB$DEF", "ÄBCDEF", "A" * 65])
def test_malformed_codes_are_rejected(raw: str) -> None:
    """Empty, short, symbol-bearing, non-ASCII and overlong codes are not well formed."""
    assert not is_well_formed_promo_code(normalize_promo_code(raw)), f"{raw!r} should be rejected"


def test_chosen_codes_must_be_long_enough_to_resist_guessing() -> None:
    """Chosen codes need at least 12 characters after normalization."""
    assert normalize_chosen_promo_code("launch-week-26") == "1AUNCHWEEK26", "12 characters pass"
    with pytest.raises(PromoCodeError, match="12-64"):
        normalize_chosen_promo_code("LAUNCH-WEEK")
    generated = generate_promo_code()
    assert normalize_chosen_promo_code(generated) == normalize_promo_code(generated), (
        "a generated code always meets the chosen-code minimum"
    )


def test_generated_codes_are_grouped_unambiguous_and_redeemable() -> None:
    """Generated codes are dash-grouped, avoid ambiguous letters and pass validation."""
    code = generate_promo_code()
    assert re.fullmatch(r"[0-9A-Z]{5}(-[0-9A-Z]{5}){3}", code), f"{code} should be 4 groups of 5"
    assert set(code.replace("-", "")) <= set(PROMO_CODE_ALPHABET), "only alphabet characters"
    assert not set("ILOU") & set(code), "I, L, O and U should never appear"
    assert is_well_formed_promo_code(normalize_promo_code(code.lower())), (
        "a lower-cased code should still be well formed"
    )
    assert generate_promo_code() != code, "two generated codes should differ"


def test_generation_maps_bytes_onto_the_alphabet() -> None:
    """Each random byte picks one alphabet character."""
    code = generate_promo_code(random_bytes=lambda n: bytes(range(n)))
    assert code == "01234-56789-ABCDE-FGHJK", "byte i should map to the i-th alphabet character"


def test_parse_utc_timestamp_reads_naive_as_utc_and_converts_offsets() -> None:
    """Naive timestamps read as UTC, offsets convert to UTC and junk is refused."""
    assert parse_utc_timestamp("2026-05-01T12:00") == NOW, "a naive timestamp should read as UTC"
    assert parse_utc_timestamp("2026-05-01T14:00+02:00") == NOW, "an offset should convert to UTC"
    with pytest.raises(PromoCodeError):
        parse_utc_timestamp("next tuesday")


def test_credit_terms() -> None:
    """Credit terms keep the amount, trim the note and set no windows."""
    terms = build_promo_code_terms(
        amount_usd=Decimal("25.50"), timed=False, max_redemptions=3, note="  launch  "
    )
    assert (terms.kind, terms.amount_usd, terms.note) == ("credit", Decimal("25.50"), "launch"), (
        "credit terms should keep the amount and trim the note"
    )
    assert terms.credit_starts_at is None and terms.redeem_ends_at is None, (
        "credit terms should set no windows by default"
    )


def test_timed_terms_stop_redemption_when_the_credit_ends() -> None:
    """Timed terms close redemption when the credit window ends."""
    terms = build_promo_code_terms(
        amount_usd=Decimal("5"), timed=True, credit_starts_at=NOW, credit_ends_at=NOW + HOUR
    )
    assert terms.kind == "timed", "timed=True should build timed terms"
    assert terms.redeem_ends_at == NOW + HOUR, "redemption should close when the credit ends"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"amount_usd": Decimal("0"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("-1"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("NaN"), "timed": False}, "positive"),
        ({"amount_usd": Decimal("1.005"), "timed": False}, "two decimal"),
        ({"amount_usd": Decimal("1000000"), "timed": False}, "at most"),
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
    """Bad amounts, windows and limits are refused with a matching reason."""
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
    """Each code state gives its refusal reason, or none when redeemable."""
    assert redeem_refusal(_code(**overrides), now=NOW) == reason, (
        f"{overrides} should give refusal {reason}"
    )


def test_timed_credit_is_granted_on_redeem_only_once_started() -> None:
    """Redeeming grants credit at once unless a timed window has not started."""
    assert is_granted_on_redeem(_code(), now=NOW), "plain credit should be granted on redeem"
    early = _code(kind="timed", credit_starts_at=NOW + HOUR, credit_ends_at=NOW + 2 * HOUR)
    assert not is_granted_on_redeem(early, now=NOW), "timed credit should wait for its window"
    assert is_granted_on_redeem(early, now=NOW + HOUR), "timed credit should grant once started"


def test_every_refusal_has_a_sentence() -> None:
    """Every refusal reason has a full-sentence description."""
    for reason in get_args(PromoRefusal):
        assert describe_refusal(reason).endswith("."), f"{reason} should end with a full stop"


def test_the_not_allowed_refusal_names_only_server_admins() -> None:
    """A channel's admins never set its budget, so they redeem no channel budget code."""
    assert describe_refusal("not_allowed") == (
        "Only a workspace or server admin can redeem that code."
    ), "the refusal must not promise channel admins a redemption"
