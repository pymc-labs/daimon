"""Calls a person allowed for the rest of one conversation from their card ("Allow for this
conversation"). Kept in this process, where the card and its turn live, for `TTL`; a restart
asks again."""

from __future__ import annotations

import time
from typing import Final

TTL: Final = 12 * 3600.0
#: Tools whose card offers it: reads as the person, each shown to the room.
SESSION_ALLOWABLE: Final = frozenset({"data_my_drive"})
_allowed: dict[str, float] = {}


def allowance_key(session: str, requester: str, tool: str) -> str:
    return f"{session}\x00{requester}\x00{tool}"


def allow(key: str) -> None:
    now = time.monotonic()
    for k in [k for k, until in _allowed.items() if until <= now]:
        del _allowed[k]
    _allowed[key] = now + TTL


def allowed(key: str) -> bool:
    return _allowed.get(key, 0.0) > time.monotonic()
