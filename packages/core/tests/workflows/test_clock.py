"""Business days in `add_duration`, the one place spec durations become times."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from daimon.core.workflows.clock import add_duration
from daimon.core.workflows.spec import parse_duration

FRIDAY = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "duration", "timezone", "expected"),
    [
        (FRIDAY, "1bd", None, datetime(2026, 11, 23, 15, 30, tzinfo=UTC)),
        (FRIDAY, "5bd", None, datetime(2026, 11, 27, 15, 30, tzinfo=UTC)),
        (datetime(2026, 11, 21, 9, tzinfo=UTC), "1bd", None, datetime(2026, 11, 23, 9, tzinfo=UTC)),
        (datetime(2026, 11, 22, 9, tzinfo=UTC), "1bd", None, datetime(2026, 11, 23, 9, tzinfo=UTC)),
        # Friday 23:00 UTC is Saturday 08:00 in Tokyo, so the next weekday there is Monday.
        (
            datetime(2026, 11, 20, 23, tzinfo=UTC),
            "1bd",
            "Asia/Tokyo",
            datetime(2026, 11, 22, 23, tzinfo=UTC),
        ),
        (FRIDAY, "2h30m", "Asia/Tokyo", datetime(2026, 11, 20, 18, tzinfo=UTC)),
    ],
)
def test_business_days_skip_weekends_in_the_spec_timezone(
    start: datetime, duration: str, timezone: str | None, expected: datetime
) -> None:
    assert add_duration(start, duration, timezone) == expected


def test_business_days_keep_the_wall_time_across_a_clock_change() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    before = datetime(2026, 10, 23, 9, tzinfo=berlin)  # Friday, summer time
    after = add_duration(before.astimezone(UTC), "1bd", "Europe/Berlin")
    assert after.astimezone(berlin).replace(tzinfo=None) == datetime(2026, 10, 26, 9)


def test_a_fixed_duration_has_no_business_days() -> None:
    with pytest.raises(ValueError, match="bd"):
        parse_duration("1bd")
