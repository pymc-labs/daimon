"""Spec time arithmetic: business days in `add_duration`, schedule slots in `next_slot`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from daimon.core.workflows.clock import add_duration, next_slot, run_times
from daimon.core.workflows.spec import ScheduleTrigger, parse_duration

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


def _slots(count: int, after: datetime, **fields: object) -> list[datetime]:
    trigger = ScheduleTrigger.model_validate({"id": "s", "kind": "schedule", **fields})
    found: list[datetime] = []
    while len(found) < count and (slot := next_slot(trigger, after)) is not None:
        found.append(slot)
        after = slot
    return found


def test_every_weeks_fires_in_the_anchor_week_and_every_nth_week_after() -> None:
    slots = _slots(
        3,
        datetime(2026, 7, 1, tzinfo=UTC),
        cron="0 9 * * 1",
        timezone="America/New_York",
        every_weeks=2,
        anchor="2026-07-22",  # a Wednesday: its week starts Monday the 20th
    )
    assert slots == [
        datetime(2026, 7, 20, 13, tzinfo=UTC),
        datetime(2026, 8, 3, 13, tzinfo=UTC),
        datetime(2026, 8, 17, 13, tzinfo=UTC),
    ], "weeks 0, 2 and 4; the week before the anchor does not fire"


def test_every_weeks_counts_across_a_year_end_and_a_clock_change() -> None:
    year_end = _slots(
        3, datetime(2026, 12, 1, tzinfo=UTC), cron="0 9 * * 1", every_weeks=2, anchor="2026-12-14"
    )
    assert year_end == [
        datetime(2026, 12, 14, 9, tzinfo=UTC),
        datetime(2026, 12, 28, 9, tzinfo=UTC),
        datetime(2027, 1, 11, 9, tzinfo=UTC),
    ], "ISO week 53 and week 1 are two Mondays apart, not a reset"
    clocks = _slots(
        3,
        datetime(2027, 2, 1, tzinfo=UTC),
        cron="0 9 * * 1",
        timezone="America/New_York",
        every_weeks=2,
        anchor="2027-02-22",
    )
    assert clocks == [
        datetime(2027, 2, 22, 14, tzinfo=UTC),
        datetime(2027, 3, 8, 14, tzinfo=UTC),
        datetime(2027, 3, 22, 13, tzinfo=UTC),
    ], "9:00 local stays 9:00 local when the clocks change on March 14"


def test_every_weeks_counts_weeks_in_the_trigger_timezone() -> None:
    first = _slots(
        2,
        datetime(2026, 7, 1, tzinfo=UTC),
        cron="0 9 * * 1",
        timezone="Pacific/Auckland",
        every_weeks=3,
        anchor="2026-07-20",
    )
    assert first == [
        datetime(2026, 7, 19, 21, tzinfo=UTC),
        datetime(2026, 8, 9, 21, tzinfo=UTC),
    ], "Monday 9:00 in Auckland is Sunday evening in UTC, and still week 0"


def test_a_dense_cron_with_a_sparse_week_does_not_walk_every_slot() -> None:
    slots = _slots(
        2, datetime(2026, 7, 1, tzinfo=UTC), cron="* * * * *", every_weeks=8, anchor="2026-07-20"
    )
    assert slots[0] == datetime(2026, 7, 20, tzinfo=UTC), "the first minute of the anchor week"
    assert slots[1] == slots[0] + timedelta(minutes=1), "then every minute of that week"


def test_a_one_time_schedule_has_one_slot_and_none_after_it() -> None:
    at = datetime(2026, 12, 1, 8, tzinfo=UTC)
    spec = {"id": "s", "kind": "schedule", "at": "2026-12-01T10:00:00+02:00"}
    trigger = ScheduleTrigger.model_validate(spec)
    assert next_slot(trigger, at - timedelta(days=1)) == at, "the time, in UTC"
    assert next_slot(trigger, at) is None, "once it has passed there is no next slot"


def test_run_times_follow_the_timezone() -> None:
    assert run_times(FRIDAY, None) == {
        "date": "2026-11-20",
        "started_at": "2026-11-20T15:30:00+00:00",
    }
    assert run_times(FRIDAY.replace(hour=23), "Asia/Tokyo")["date"] == "2026-11-21"
