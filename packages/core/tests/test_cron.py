"""Pure-function tests for `daimon.core.cron.next_slot_at_or_after`.

No DB, no clocks, no I/O. Three cases: UTC boundary +1s, IANA-tz mapping
to UTC, and DST spring-forward.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from daimon.core.cron import next_slot_at_or_after


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
