"""What the chat connectors share: config lists, and messages grouped into record windows."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Final, cast

from daimon.data.sync.connector import Json

WINDOW: Final = 20_000
"""Characters of message lines in one record before a day splits into parts."""
MAX_DAYS: Final = 3650
REWALK_DAYS: Final = 7
"""Days re-read by default for edits and deletes."""
LOST: Final = timedelta(days=30)
"""How long a channel the bot lost keeps its records, hidden, for access to come back: the
usual soft-delete window."""


class Unreadable(Exception):
    """The bot may not read a room after all: it is left as it is. `code`: provider error code."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def lost_for_good(lost: Json, missing: Collection[str], now: datetime) -> set[str]:
    """Track channels the bot cannot read now in `lost` (`{id: first missed}`): one back is
    forgotten, and those missed for `LOST` are returned (and forgotten) for their records to go.
    Until then they stay, hidden: a channel the bot lost publishes no readers."""
    for k in [k for k in lost if k not in missing]:
        del lost[k]
    for k in missing:
        lost.setdefault(k, now.isoformat())
    gone = {k for k in missing if now - datetime.fromisoformat(str(lost[k])) >= LOST}
    for k in gone:
        del lost[k]
    return gone


def rows(value: object) -> list[Json]:
    """A JSON array of objects; anything else (a missing key) is empty."""
    return cast("list[Json]", value) if isinstance(value, list) else []


def obj(value: object) -> Json:
    """A JSON object; anything else (a missing key) is empty."""
    return cast("Json", value) if isinstance(value, dict) else {}


def names(config: Json, key: str) -> list[str]:
    """A list setting, given as a list or as comma-separated text; a leading `#` is dropped."""
    value: object = config.get(key) or []
    items = value.split(",") if isinstance(value, str) else cast("list[object]", value)
    return [s for x in items if (s := str(x).strip().lstrip("#"))]


def whole_days(config: Json, default: int) -> int:
    """`rewalk_days`: whole days to re-read for edits, 1 to `MAX_DAYS`."""
    raw = str(config.get("rewalk_days") or "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= MAX_DAYS):
        raise ValueError(f"config.rewalk_days must be a whole number of days, 1 to {MAX_DAYS}")
    return int(raw)


def split(lines: list[str], size: int = WINDOW) -> list[list[int]]:
    """Line indexes in runs of at most `size` characters (a long line stands alone)."""
    parts: list[list[int]] = []
    n = 0
    for i, line in enumerate(lines):
        if not parts or n + len(line) > size:
            parts.append([])
            n = 0
        parts[-1].append(i)
        n += len(line) + 1
    return parts


def authors_of(who: list[str]) -> str:
    return ", ".join(who[:3]) + (f" +{len(who) - 3}" if len(who) > 3 else "")
