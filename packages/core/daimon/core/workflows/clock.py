"""Time arithmetic for workflow specs. Every timer, timeout and reminder a spec sets ends at
`add_duration`, so business days count the same everywhere; every schedule slot, at
`next_slot`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from daimon.core.cron import next_slot_at_or_after, validated_next_slot
from daimon.core.workflows.spec import ScheduleTrigger, business_days, parse_duration


def add_duration(start: datetime, duration: str, timezone: str | None = None) -> datetime:
    """`start` plus a spec duration. `Nbd` lands N weekdays (Mon-Fri, no holidays) later at
    the same wall time in `timezone` (UTC when None): Friday plus 1bd is Monday."""
    days = business_days(duration)
    if days is None:
        return start + parse_duration(duration)
    local = start.astimezone(ZoneInfo(timezone) if timezone else UTC)
    day = local.date()
    while days:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days -= 1
    return datetime.combine(day, local.timetz()).astimezone(start.tzinfo)


#: Cron slots tried before a multi-week schedule gives up (a monthly cron in a 1-in-8 week).
_MAX_SLOT_TRIES = 500


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_slot(trigger: ScheduleTrigger, after: datetime) -> datetime | None:
    """The first slot strictly after `after`, in UTC, or None when there is none (a one-time
    `at` already past). A cron with `every_weeks` fires only in the weeks `n * every_weeks`
    after the ISO week of `anchor`, counted in the trigger's timezone. Raises
    `InvalidScheduleError` for a bad zone or cron."""
    if trigger.at is not None:
        return trigger.at.astimezone(UTC) if trigger.at > after else None
    zone = trigger.timezone or "UTC"
    slot = validated_next_slot(trigger.cron or "", zone, after)
    if trigger.every_weeks is None or trigger.anchor is None:
        return slot
    tz = ZoneInfo(zone)
    first = _week_start(trigger.anchor)
    for _ in range(_MAX_SLOT_TRIES):
        week = (_week_start(slot.astimezone(tz).date()) - first).days // 7
        if week >= 0 and week % trigger.every_weeks == 0:
            return slot
        target = 0 if week < 0 else week + trigger.every_weeks - week % trigger.every_weeks
        start = datetime.combine(first + timedelta(weeks=target), time(), tzinfo=tz)
        # Two seconds: `next_slot_at_or_after` adds one itself, and a slot at midnight counts.
        before = start.astimezone(UTC) - timedelta(seconds=2)
        slot = next_slot_at_or_after(trigger.cron or "", zone, before)
    return None


def run_times(started_at: datetime, timezone: str | None) -> dict[str, str]:
    """`run.date` and `run.started_at` as the spec's timezone (UTC when None) shows them."""
    local = started_at.astimezone(ZoneInfo(timezone) if timezone else UTC)
    return {"date": local.date().isoformat(), "started_at": local.isoformat()}
