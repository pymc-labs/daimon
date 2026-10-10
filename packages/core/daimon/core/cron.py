"""Pure cron-slot computation.

Lives in its own module (not `scheduler.py`) so `stores.routines` can import it
without creating a `scheduler` <-> `stores.routines` import cycle: the scheduler
imports the stores, and the stores need next-slot computation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import (
    croniter,  # pyright: ignore[reportMissingTypeStubs]  # croniter ships untyped at this version
)


def next_slot_at_or_after(cron_expr: str, tz: str, after: datetime) -> datetime:
    """First cron slot strictly > `after`, evaluated in IANA tz. Returns UTC.

    +1s on the input avoids landing on `after` itself; croniter `get_next`
    precision around second boundaries is fuzzy.
    """
    after_utc = after.astimezone(UTC)
    base = (after_utc + timedelta(seconds=1)).astimezone(ZoneInfo(tz))
    nxt_local: datetime = croniter(cron_expr, base).get_next(datetime)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # croniter untyped
    if nxt_local.tzinfo is None:
        nxt_local = nxt_local.replace(tzinfo=ZoneInfo(tz))
    nxt_utc = nxt_local.astimezone(UTC)
    if nxt_utc <= after_utc:
        raise ValueError("cron returned a slot no later than the input")
    return nxt_utc


class InvalidScheduleError(ValueError):
    """A timezone or cron expression a routine cannot be scheduled on."""


def validated_next_slot(cron_expr: str, tz: str, after: datetime) -> datetime:
    """`next_slot_at_or_after` for user input: a bad zone or cron raises `InvalidScheduleError`."""
    try:
        ZoneInfo(tz)
    except (KeyError, ValueError) as error:  # ZoneInfoNotFoundError is a KeyError
        raise InvalidScheduleError(f"unknown timezone: {tz!r}") from error
    try:
        return next_slot_at_or_after(cron_expr, tz, after)
    except (KeyError, ValueError) as error:  # croniter raises both
        raise InvalidScheduleError(f"invalid cron expression: {cron_expr!r}") from error
