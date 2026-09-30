"""Promo code terms: normalization, hashing, generation, validation, redeemability.

Pure: no clock, no randomness source of its own, no I/O. The shell
(`daimon.core.promo_credit`) and the operator surfaces call these.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from daimon.core.stores.domain import PromoCodeKind, PromoCodeRow

# Crockford base32: no I, L, O or U, so a code read aloud or retyped has no
# look-alike characters. 256 is a multiple of 32, so a byte maps without bias.
PROMO_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUP_COUNT = 4
_GROUP_LENGTH = 5  # 20 characters = 100 bits of entropy
_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6,64}$")
MIN_CHOSEN_CODE_LENGTH = 12
_SEPARATORS = re.compile(r"[\s-]+")
_LOOK_ALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})
# Largest amount per redemption. The ledger holds Numeric(12, 6), so a grant
# above this would overflow at write time instead of at creation.
MAX_PROMO_AMOUNT_USD = Decimal("999999.99")

PromoRefusal = Literal[
    "invalid", "revoked", "not_started", "expired", "exhausted", "already_redeemed", "throttled"
]
"""Why a redemption was refused."""

_REFUSAL_TEXT: dict[PromoRefusal, str] = {
    "invalid": "That code is not valid. Check it and try again.",
    "revoked": "That code is no longer active.",
    "not_started": "That code cannot be redeemed yet.",
    "expired": "That code has expired.",
    "exhausted": "That code has been fully redeemed.",
    "already_redeemed": "That code was already redeemed here.",
    "throttled": "Too many failed attempts. Try again in a few minutes.",
}


def describe_refusal(reason: PromoRefusal) -> str:
    """One plain sentence for a refused redemption, shared by every surface."""
    return _REFUSAL_TEXT[reason]


class PromoCodeError(ValueError):
    """Promo code terms an operator supplied are inconsistent."""


def normalize_promo_code(raw: str) -> str:
    """The form a code is hashed in: no case, no separators, Crockford look-alikes folded.

    O reads as 0 and I or L as 1, so a code misread from print still matches.
    """
    return _SEPARATORS.sub("", raw).upper().translate(_LOOK_ALIKES)


def is_well_formed_promo_code(normalized: str) -> bool:
    return _CODE_PATTERN.fullmatch(normalized) is not None


def normalize_chosen_promo_code(raw: str) -> str:
    """Normalize a code an operator picked, refusing one short enough to guess.

    Generated codes carry 100 bits; a chosen code must be at least
    ``MIN_CHOSEN_CODE_LENGTH`` characters, about 60 bits.
    """
    normalized = normalize_promo_code(raw)
    if len(normalized) < MIN_CHOSEN_CODE_LENGTH or not is_well_formed_promo_code(normalized):
        raise PromoCodeError(
            f"a chosen code must be {MIN_CHOSEN_CODE_LENGTH}-64 letters or digits"
            " (dashes and spaces ignored)"
        )
    return normalized


def hash_promo_code(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()


def generate_promo_code(*, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """A fresh high-entropy code in dash-separated groups, e.g. ``7KQ2M-...``."""
    raw = random_bytes(_GROUP_COUNT * _GROUP_LENGTH)
    chars = "".join(PROMO_CODE_ALPHABET[byte % len(PROMO_CODE_ALPHABET)] for byte in raw)
    return "-".join(chars[i : i + _GROUP_LENGTH] for i in range(0, len(chars), _GROUP_LENGTH))


def parse_utc_timestamp(value: str) -> datetime:
    """ISO 8601 in, aware UTC out. A value without an offset is read as UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise PromoCodeError(f"not an ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclasses.dataclass(frozen=True)
class PromoCodeTerms:
    """Validated terms of a new promo code, ready for the store."""

    amount_usd: Decimal
    kind: PromoCodeKind
    note: str | None
    credit_starts_at: datetime | None
    credit_ends_at: datetime | None
    redeem_starts_at: datetime | None
    redeem_ends_at: datetime | None
    max_redemptions: int | None


def build_promo_code_terms(
    *,
    amount_usd: Decimal,
    timed: bool,
    credit_starts_at: datetime | None = None,
    credit_ends_at: datetime | None = None,
    redeem_starts_at: datetime | None = None,
    redeem_ends_at: datetime | None = None,
    max_redemptions: int | None = None,
    note: str | None = None,
) -> PromoCodeTerms:
    """Validate operator input. A timed code's redemption ends with its credit by default."""
    if not amount_usd.is_finite() or amount_usd <= 0:
        raise PromoCodeError("amount must be a positive dollar amount")
    if amount_usd > MAX_PROMO_AMOUNT_USD:
        raise PromoCodeError(f"amount must be at most ${MAX_PROMO_AMOUNT_USD:,}")
    exponent = amount_usd.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise PromoCodeError("amount must have at most two decimal places")
    if timed:
        if credit_starts_at is None or credit_ends_at is None:
            raise PromoCodeError("a timed code needs both a credit start and a credit end")
        if credit_starts_at >= credit_ends_at:
            raise PromoCodeError("the credit start must be before the credit end")
        redeem_ends_at = redeem_ends_at or credit_ends_at
        if redeem_ends_at > credit_ends_at:
            raise PromoCodeError("a timed code cannot stay redeemable after its credit ends")
    elif credit_starts_at is not None or credit_ends_at is not None:
        raise PromoCodeError("a credit start or end needs a timed code")
    if (
        redeem_starts_at is not None
        and redeem_ends_at is not None
        and redeem_starts_at >= redeem_ends_at
    ):
        raise PromoCodeError("the redemption start must be before the redemption end")
    if max_redemptions is not None and max_redemptions < 1:
        raise PromoCodeError("max redemptions must be at least 1")
    return PromoCodeTerms(
        amount_usd=amount_usd,
        kind="timed" if timed else "credit",
        note=(note or "").strip() or None,
        credit_starts_at=credit_starts_at,
        credit_ends_at=credit_ends_at,
        redeem_starts_at=redeem_starts_at,
        redeem_ends_at=redeem_ends_at,
        max_redemptions=max_redemptions,
    )


def redeem_refusal(code: PromoCodeRow, *, now: datetime) -> PromoRefusal | None:
    """Why ``code`` cannot be redeemed at ``now``, or None when it can."""
    if code.revoked_at is not None:
        return "revoked"
    if code.redeem_starts_at is not None and now < code.redeem_starts_at:
        return "not_started"
    if code.redeem_ends_at is not None and now >= code.redeem_ends_at:
        return "expired"
    if code.credit_ends_at is not None and now >= code.credit_ends_at:
        return "expired"
    if code.max_redemptions is not None and code.redeemed_count >= code.max_redemptions:
        return "exhausted"
    return None


def is_granted_on_redeem(code: PromoCodeRow, *, now: datetime) -> bool:
    """Credit codes and started timed codes reach the ledger at redemption."""
    return code.credit_starts_at is None or code.credit_starts_at <= now
