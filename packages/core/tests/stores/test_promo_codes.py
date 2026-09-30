"""Store queries over promo codes that the shells do not cover end to end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.stores import promo_codes as promo_store
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


async def test_no_code_is_redeemable_on_a_fresh_deployment(db_session: AsyncSession) -> None:
    """An empty table answers no, so join messages stay unchanged."""
    assert not await promo_store.has_redeemable_promo_code(db_session, now=NOW), (
        "with no promo codes nothing should advertise redemption"
    )


@pytest.mark.parametrize(
    ("terms", "redeemable"),
    [
        ({}, True),
        ({"redeem_starts_at": NOW + HOUR}, False),
        ({"redeem_ends_at": NOW}, False),
        ({"timed": True, "credit_starts_at": NOW - 2 * HOUR, "credit_ends_at": NOW}, False),
        ({"timed": True, "credit_starts_at": NOW + HOUR, "credit_ends_at": NOW + 2 * HOUR}, True),
    ],
)
async def test_has_redeemable_promo_code_follows_the_redemption_windows(
    db_session: AsyncSession, terms: dict[str, Any], redeemable: bool
) -> None:
    """Only a code inside its redemption window, with credit still to come, counts."""
    kwargs: dict[str, Any] = {"timed": False, **terms}
    await promo_store.insert_promo_code(
        db_session, code_hash="h", terms=build_promo_code_terms(amount_usd=Decimal("5"), **kwargs)
    )
    assert await promo_store.has_redeemable_promo_code(db_session, now=NOW) is redeemable, (
        f"terms {terms} should make the code redeemable={redeemable}"
    )


async def test_a_revoked_code_is_not_redeemable(db_session: AsyncSession) -> None:
    """Revoking the only code turns the answer back to no."""
    code = await promo_store.insert_promo_code(
        db_session,
        code_hash="h",
        terms=build_promo_code_terms(amount_usd=Decimal("5"), timed=False),
    )
    assert code is not None, "the insert should return the new row"
    await promo_store.revoke_promo_code(db_session, promo_code_id=code.id, now=NOW)
    assert not await promo_store.has_redeemable_promo_code(db_session, now=NOW), (
        "a revoked code should not count"
    )
