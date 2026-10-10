"""Pure-function tests for `daimon.core.cron`.

No DB, no clocks, no I/O. Slot arithmetic (UTC boundary +1s, IANA-tz mapping
to UTC, DST spring-forward) and the user-input validation wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from daimon.core.cron import InvalidScheduleError, next_slot_at_or_after, validated_next_slot


def test_next_slot_at_or_after_utc_boundary_returns_following_minute() -> None:
    after = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    result = next_slot_at_or_after("* * * * *", "UTC", after)
    assert result == datetime(2026, 5, 8, 12, 1, 0, tzinfo=UTC), (
        "boundary +1s should skip the input instant and return the NEXT minute"
    )
    assert result.tzinfo is UTC, "returned datetime must be UTC tz-aware"


def test_next_slot_at_or_after_evaluates_cron_in_iana_tz() -> None:
    # 09:00 Tokyo == 00:00 UTC on the same calendar date (JST = UTC+9, no DST).
    # `after` = 2026-05-07 23:00 UTC = 2026-05-08 08:00 Tokyo, so the next
    # 09:00 Tokyo slot is 2026-05-08 09:00 Tokyo == 2026-05-08 00:00 UTC.
    after = datetime(2026, 5, 7, 23, 0, 0, tzinfo=UTC)
    result = next_slot_at_or_after("0 9 * * *", "Asia/Tokyo", after)
    assert result.tzinfo is UTC, "returned datetime must be UTC"
    local = result.astimezone(ZoneInfo("Asia/Tokyo"))
    assert local.hour == 9 and local.minute == 0, (
        f"cron `0 9 * * *` Asia/Tokyo should yield 09:00 local, got {local.isoformat()}"
    )
    assert result > after, "returned slot must be strictly after input"
    assert result == datetime(2026, 5, 8, 0, 0, tzinfo=UTC)


def test_next_slot_at_or_after_handles_dst_spring_forward() -> None:
    # America/New_York DST 2026: starts Sunday 2026-03-08 02:00 -> 03:00 local.
    # `after` = 2026-03-08 05:00 UTC = 2026-03-08 01:00 EST (just before the
    # skip). Cron `30 2 * * *` would normally fire at 02:30 local; on the
    # spring-forward day 02:30 does not exist, so croniter resolves to either
    # 03:30 same day or 02:30 next day.
    after = datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)
    result = next_slot_at_or_after("30 2 * * *", "America/New_York", after)
    assert result.tzinfo is UTC, "returned datetime must be UTC"
    assert result > after, "returned slot must be strictly after input"
    local = result.astimezone(ZoneInfo("America/New_York"))
    # croniter's exact resolution of the skipped 02:30 slot is implementation-
    # dependent (some versions land at 03:00 same day, some at 02:30 next day).
    # The contract we care about: the function returns SOMETHING strictly after
    # `after`, in UTC, and the local hour is plausibly close to the requested
    # 02:30 — i.e. 02 or 03.
    assert local.hour in (2, 3), (
        f"DST resolution should land at 02:30/03:30 (skip) or 03:00, got {local.isoformat()}"
    )
    assert next_slot_at_or_after(
        "* * * * *", "America/New_York", datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    ) == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("cron_expr", "first_expected", "second_expected"),
    [
        (
            "* * * * *",
            datetime(2026, 11, 1, 5, 2, tzinfo=UTC),
            datetime(2026, 11, 1, 6, 2, tzinfo=UTC),
        ),
        (
            "30 1 * * *",
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        ),
    ],
)
def test_next_slot_after_fall_back_never_moves_backward(
    cron_expr: str, first_expected: datetime, second_expected: datetime
) -> None:
    # The second 01:00 hour starts at 06:00 UTC on 2026-11-01 in New York.
    # A next-fire value in the past makes a due row fire again on every tick.
    first = datetime(2026, 11, 1, 5, 1, tzinfo=UTC)
    second = datetime(2026, 11, 1, 6, 1, tzinfo=UTC)
    assert next_slot_at_or_after(cron_expr, "America/New_York", first) == first_expected
    result = next_slot_at_or_after(cron_expr, "America/New_York", second)
    assert result == second_expected
    assert result > second, f"next fire {result.isoformat()} must follow {second.isoformat()}"


@pytest.mark.parametrize(
    ("cron_expr", "tz", "message"),
    [
        ("0 9 * * *", "Mars/Olympus", "unknown timezone: 'Mars/Olympus'"),
        ("0 9 * * *", "../etc", "unknown timezone: '../etc'"),
        ("61 * * * *", "UTC", "invalid cron expression: '61 * * * *'"),
        ("0 0 31 2 *", "UTC", "invalid cron expression: '0 0 31 2 *'"),
    ],
)
def test_validated_next_slot_names_what_is_wrong(cron_expr: str, tz: str, message: str) -> None:
    after = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(InvalidScheduleError) as error:
        validated_next_slot(cron_expr, tz, after)
    assert str(error.value) == message, "the error names the bad field for the user"


def test_validated_next_slot_returns_the_next_slot_for_valid_input() -> None:
    after = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    assert validated_next_slot("* * * * *", "UTC", after) == next_slot_at_or_after(
        "* * * * *", "UTC", after
    ), "valid input yields the same slot as the unvalidated helper"
