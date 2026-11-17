"""Time windows shared by channel budgets and support plans. Pure.

A window is `monthly` (the current UTC calendar month), `total` (since
`starts_at`, or ever) or `fixed` (`[starts_at, ends_at)`). Both features store
the same three columns under the same CHECKs, so the period, activity and
wording rules live here once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, get_args

from daimon.core.errors import DaimonError

BudgetWindow = Literal["monthly", "total", "fixed"]
WINDOWS: tuple[BudgetWindow, ...] = get_args(BudgetWindow)
WindowState = Literal["active", "not_started", "ended"]


class WindowError(DaimonError):
    """A window that cannot be stored; the message says what to fix."""


class Windowed(Protocol):
    """Anything stored with the three window columns."""

    @property
    def window(self) -> BudgetWindow: ...
    @property
    def starts_at(self) -> datetime | None: ...
    @property
    def ends_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class WindowBounds:
    """A validated window: bounds as aware UTC datetimes."""

    window: BudgetWindow
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class WindowPeriod:
    """The half-open range `[since, until)` a window counts; None is unbounded."""

    since: datetime | None
    until: datetime | None


def parse_instant(value: str | None, *, name: str) -> datetime | None:
    """ISO 8601; a value without an offset is read as UTC."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise WindowError(f"{name} must be an ISO 8601 date or time, got {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def check_window(
    window: str, *, starts_at: datetime | None, ends_at: datetime | None
) -> WindowBounds:
    """The table CHECKs, as messages a person can act on."""
    if window not in WINDOWS:
        raise WindowError(f"window must be one of {', '.join(WINDOWS)}")
    if window == "fixed":
        if starts_at is None or ends_at is None:
            raise WindowError("a fixed window needs both starts_at and ends_at")
        if starts_at >= ends_at:
            raise WindowError("starts_at must be before ends_at")
        return WindowBounds("fixed", starts_at, ends_at)
    if window == "total":
        if ends_at is not None:
            raise WindowError("ends_at is only for a fixed window")
        return WindowBounds("total", starts_at, None)
    if starts_at is not None or ends_at is not None:
        raise WindowError("a monthly window takes no starts_at or ends_at")
    return WindowBounds("monthly", None, None)


def parse_window(
    window: str, *, starts_at: str | None = None, ends_at: str | None = None
) -> WindowBounds:
    """`check_window` over text bounds from a tool, the CLI or the web."""
    if window not in WINDOWS:
        raise WindowError(f"window must be one of {', '.join(WINDOWS)}")
    return check_window(
        window,
        starts_at=parse_instant(starts_at, name="starts_at"),
        ends_at=parse_instant(ends_at, name="ends_at"),
    )


def window_period(item: Windowed, *, now: datetime) -> WindowPeriod:
    """`monthly`: this UTC month. `total`: since `starts_at`, or ever. `fixed`: its range."""
    if item.window == "monthly":
        return WindowPeriod(since=datetime(now.year, now.month, 1, tzinfo=UTC), until=None)
    if item.window == "total":
        return WindowPeriod(since=item.starts_at, until=None)
    return WindowPeriod(since=item.starts_at, until=item.ends_at)


def window_state(item: Windowed, *, now: datetime) -> WindowState:
    """`not_started` before `starts_at`, `ended` from a fixed window's `ends_at` on."""
    if item.window == "monthly":
        return "active"
    if item.starts_at is not None and now < item.starts_at:
        return "not_started"
    if item.ends_at is not None and now >= item.ends_at:
        return "ended"
    return "active"


def is_window_active(item: Windowed, *, now: datetime) -> bool:
    """A window counts from `starts_at`, if set, and a fixed one until `ends_at`, exclusive."""
    return window_state(item, now=now) == "active"


def instant_label(value: datetime) -> str:
    if value.hour == value.minute == value.second == 0:
        return f"{value:%Y-%m-%d}"
    return f"{value:%Y-%m-%d %H:%M} UTC"


def window_label(item: Windowed, *, started: bool = True) -> str:
    """The window in words, e.g. `monthly`, `since 2026-07-01`, `2026-07-01 until 2026-07-03`.

    A total window that has not `started` reads `from 2026-07-01`. A fixed
    window's end is exclusive, hence "until".
    """
    if item.window == "monthly":
        return "monthly"
    if item.starts_at is None:
        return "total"
    if item.window == "total" or item.ends_at is None:
        return f"{'since' if started else 'from'} {instant_label(item.starts_at)}"
    return f"{instant_label(item.starts_at)} until {instant_label(item.ends_at)}"
