"""Time arithmetic for workflow specs. Every timer, timeout and reminder a spec sets ends at
`add_duration`, so business days count the same everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from daimon.core.workflows.spec import business_days, parse_duration


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
