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
    after_local = after.astimezone(ZoneInfo(tz))
    base = after_local + timedelta(seconds=1)
    nxt_local: datetime = croniter(cron_expr, base).get_next(datetime)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # croniter untyped
    if nxt_local.tzinfo is None:
        nxt_local = nxt_local.replace(tzinfo=ZoneInfo(tz))
    return nxt_local.astimezone(UTC)
