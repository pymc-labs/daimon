"""A routine's schedule in words, for the few cron shapes that say exactly one thing.

Only a schedule whose words cannot mislead is converted: every day at a time,
weekdays at a time, one named day at a time, and every hour on the hour. Any
other expression (steps, lists, ranges other than Monday to Friday, a day of
the month, six fields) comes back as the cron text itself. The timezone is
always kept, so a reader never guesses whose 09:00 it is.

Pure: no clock, no I/O, no croniter. It reads the expression, never runs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["routine_phrase", "schedule_words"]

_DAY_NAMES: Final[tuple[str, ...]] = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
_DAY_ABBREVIATIONS: Final[dict[str, int]] = {
    name[:3].upper(): index for index, name in enumerate(_DAY_NAMES)
}
_NUMBER = re.compile(r"\d{1,2}")


@dataclass(frozen=True, slots=True)
class _Simple:
    kind: Literal["daily", "weekdays", "weekly", "hourly"]
    time: str = ""
    day: str = ""


def _number(field: str, *, upper: int) -> int | None:
    if _NUMBER.fullmatch(field) is None:
        return None
    value = int(field)
    return value if value <= upper else None


def _day(field: str) -> int | None:
    """One day of the week, as a number 0-7 (both ends Sunday) or a three-letter name."""
    named = _DAY_ABBREVIATIONS.get(field.upper())
    if named is not None:
        return named
    number = _number(field, upper=7)
    return None if number is None else number % 7


def _simple(cron_expr: str) -> _Simple | None:
    fields = cron_expr.split()
    if len(fields) != 5:
        return None
    minute_field, hour_field, day_of_month, month, day_of_week = fields
    if day_of_month != "*" or month != "*":
        return None
    minute = _number(minute_field, upper=59)
    if minute is None:
        return None
    if hour_field == "*":
        if minute == 0 and day_of_week == "*":
            return _Simple("hourly")
        return None
    hour = _number(hour_field, upper=23)
    if hour is None:
        return None
    time = f"{hour:02d}:{minute:02d}"
    if day_of_week == "*":
        return _Simple("daily", time)
    if day_of_week.upper() in ("1-5", "MON-FRI"):
        return _Simple("weekdays", time)
    day = _day(day_of_week)
    if day is None:
        return None
    return _Simple("weekly", time, _DAY_NAMES[day])


def schedule_words(cron_expr: str, timezone: str) -> str:
    """`0 9 * * *` in `UTC` as "every day at 09:00 UTC"; anything unsure as the cron text.

    The other words are "weekdays at 09:00 UTC", "every Monday at 09:00 UTC"
    and "every hour (UTC)"; the fallback is "0 9 * * 1,3 (UTC)".
    """
    simple = _simple(cron_expr)
    if simple is None:
        return f"{cron_expr.strip()} ({timezone})"
    if simple.kind == "hourly":
        return f"every hour ({timezone})"
    if simple.kind == "daily":
        return f"every day at {simple.time} {timezone}"
    if simple.kind == "weekdays":
        return f"weekdays at {simple.time} {timezone}"
    return f"every {simple.day} at {simple.time} {timezone}"


def routine_phrase(cron_expr: str, timezone: str) -> str:
    """The routine named by its schedule: "daily routine at 09:00 UTC".

    The other words are "weekday routine at 09:00 UTC", "Monday routine at
    09:00 UTC" and "hourly routine (UTC)"; anything unsure is "routine
    (0 9 * * 1,3, UTC)".
    """
    simple = _simple(cron_expr)
    if simple is None:
        return f"routine ({cron_expr.strip()}, {timezone})"
    if simple.kind == "hourly":
        return f"hourly routine ({timezone})"
    if simple.kind == "daily":
        return f"daily routine at {simple.time} {timezone}"
    if simple.kind == "weekdays":
        return f"weekday routine at {simple.time} {timezone}"
    return f"{simple.day} routine at {simple.time} {timezone}"
