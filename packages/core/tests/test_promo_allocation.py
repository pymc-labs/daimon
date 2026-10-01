"""Pure allocation of spend onto timed promo grants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core.promo_allocation import (
    TimedGrant,
    relevant_grants,
    remaining_timed_credit,
    spend_bounds,
)

T0 = datetime(2026, 5, 1, tzinfo=UTC)
H = timedelta(hours=1)


def _grant(start: int, end: int, amount: str = "10") -> TimedGrant:
    return TimedGrant(uuid.uuid4(), Decimal(amount), T0 + start * H, T0 + end * H)


def _remaining(grants: list[TimedGrant], spend_at: dict[int, str], *, horizon: int):
    bounds = spend_bounds(grants, horizon=T0 + horizon * H)
    spend = [
        sum(
            (Decimal(v) for k, v in spend_at.items() if lo <= T0 + k * H < hi),
            Decimal("0"),
        )
        for lo, hi in zip(bounds, bounds[1:], strict=False)
    ]
    return remaining_timed_credit(grants, bounds=bounds, spend=spend)


def test_spend_inside_the_window_draws_the_grant_down() -> None:
    """Spend inside a grant's window reduces what is left of it."""
    grant = _grant(0, 10)
    left = _remaining([grant], {2: "3", 5: "4"}, horizon=10)
    assert left[grant.promo_code_id] == Decimal("3"), "$7 of spend should leave $3 of $10"


def test_spend_never_takes_a_grant_below_zero() -> None:
    """Overspend empties a grant without making it negative."""
    grant = _grant(0, 10)
    assert _remaining([grant], {1: "25"}, horizon=10)[grant.promo_code_id] == 0, (
        "overspend should stop the grant at zero"
    )


def test_overlapping_grants_are_drawn_earliest_ending_first() -> None:
    """Where grants overlap, the one ending first is spent first."""
    long, short = _grant(0, 10), _grant(2, 6)
    left = _remaining([long, short], {1: "2", 3: "12"}, horizon=10)
    # Hour 1: only `long` is live. Hour 3: `short` ends first, so it empties before `long`.
    assert left[short.promo_code_id] == 0, "the earlier-ending grant should empty first"
    assert left[long.promo_code_id] == Decimal("6"), "the longer grant should cover the rest"


def test_spend_outside_every_window_is_ignored() -> None:
    """Intervals cover only grant windows, so spend outside them is not counted."""
    grant = _grant(2, 4)
    bounds = spend_bounds([grant], horizon=T0 + 10 * H)
    assert bounds == [T0 + 2 * H, T0 + 4 * H], "bounds should cover only the grant window"
    assert remaining_timed_credit([grant], bounds=bounds, spend=[Decimal("1")]) == {
        grant.promo_code_id: Decimal("9")
    }, "only spend inside the window should count"


def test_horizon_cuts_the_last_interval() -> None:
    """The horizon truncates the final interval."""
    grant = _grant(0, 10)
    assert spend_bounds([grant], horizon=T0 + 4 * H) == [T0, T0 + 4 * H], (
        "bounds should stop at the horizon"
    )


def test_relevant_grants_keep_whole_overlap_clusters() -> None:
    """A target pulls in every grant overlapping it, directly or through others."""
    a, b, c, later = _grant(0, 4), _grant(3, 8), _grant(7, 9), _grant(9, 12)
    assert set(
        relevant_grants([later, c, b, a], targets={a.promo_code_id}, horizon=T0 + 20 * H)
    ) == {
        a,
        b,
        c,
    }, "the whole chain of overlapping grants should be kept"
    assert relevant_grants([a, later], targets={later.promo_code_id}, horizon=T0 + 20 * H) == [
        later
    ], "a grant that overlaps no target should be dropped"


def test_relevant_grants_drop_grants_not_started_by_the_horizon() -> None:
    """Grants that start at or after the horizon are dropped."""
    a, b = _grant(0, 4), _grant(2, 6)
    assert relevant_grants([a, b], targets={a.promo_code_id}, horizon=T0 + 2 * H) == [a], (
        "a grant starting at the horizon should be dropped"
    )


def test_spend_must_match_the_intervals() -> None:
    """A spend list that does not match the intervals is refused."""
    grant = _grant(0, 1)
    with pytest.raises(ValueError, match="one amount per interval"):
        remaining_timed_credit([grant], bounds=[T0, T0 + H], spend=[])
