"""Wait out a burst of direct messages, so one turn answers all of them.

The first message of a burst opens a window; each later message from the same person in
the same conversation joins it. The window closes once nothing new has arrived for the
configured wait, or ten seconds after it opened. A turn starts only then.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Hashable, Sequence
from time import monotonic

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature
from daimon.core.stores.pending_skill_adds import is_affirmative

MAX_WAIT_S = 10.0
"""The longest a burst is held, however long it keeps going."""
_SLICE_S = 1.0
"""A wait wakes at least this often, so a shutdown is noticed within a second."""


async def debounce_seconds(
    sessions: Sessions, settings: Settings | None, tenant_id: uuid.UUID, platform: str
) -> float:
    """The wait before a direct message starts a turn; 0 answers each message at once."""
    found = await place_feature(
        sessions, settings, FeaturePlace.of(tenant_id, platform, None), "dm_debounce_seconds"
    )
    return float(found.value)


def bypasses_wait(text: str) -> bool:
    """An approval reply ("yes") is never held: it answers a question already asked."""
    return is_affirmative(text)


def split_approvals[T](items: Sequence[T], text_of: Callable[[T], str]) -> list[list[T]]:
    """One turn per run of ordinary messages; an approval reply stands alone."""
    groups: list[list[T]] = []
    run: list[T] = []
    for item in items:
        if bypasses_wait(text_of(item)):
            if run:
                groups.append(run)
                run = []
            groups.append([item])
        else:
            run.append(item)
    if run:
        groups.append(run)
    return groups


class DmWindow[T]:
    """The messages of one burst, in the order they arrived."""

    def __init__(self, first: T) -> None:
        self.items: list[T] = [first]
        self._opened = self._last = monotonic()
        self._wake = asyncio.Event()
        self._flush = False
        self._seen = 0

    def add(self, item: T, *, flush: bool = False) -> None:
        """Join the burst. Never awaits, so a message cannot slip between a close and a join."""
        self.items.append(item)
        self._last = monotonic()
        self._flush = self._flush or flush
        self._wake.set()

    def flush(self) -> None:
        """End the wait now (an approval reply arrived, or the process is stopping)."""
        self._flush = True
        self._wake.set()

    async def settle(
        self,
        seconds: float,
        *,
        on_arrival: Callable[[T], Awaitable[None]] | None = None,
        stop: Callable[[], bool] = lambda: False,
    ) -> list[T]:
        """Wait until nothing new has arrived for `seconds`, or the cap; the whole burst.

        The cap counts from the window's first message, so the lookups before the wait
        do not extend it. With `seconds` of 0 the burst is whatever already joined.
        `on_arrival` runs once for each held message as soon as it is seen: every one
        while a wait applies, and otherwise those that joined behind the first.
        """
        cap = self._opened + MAX_WAIT_S
        while True:
            while on_arrival is not None and self._seen < len(self.items):
                item = self.items[self._seen]
                self._seen += 1
                if seconds > 0 or self._seen > 1:
                    await on_arrival(item)
            if seconds <= 0 or self._flush or stop():
                break
            remaining = min(self._last + seconds, cap) - monotonic()
            if remaining <= 0:
                break
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), min(remaining, _SLICE_S))
        return list(self.items)


class DmDebouncer[K: Hashable, T]:
    """The open window of each conversation."""

    def __init__(self) -> None:
        self._open: dict[K, DmWindow[T]] = {}
        self._stopping = False

    def join(self, key: K, item: T, *, flush: bool = False) -> bool:
        """Add `item` to the conversation's open window; False when none is open."""
        window = self._open.get(key)
        if window is None:
            return False
        window.add(item, flush=flush)
        return True

    def open(self, key: K, first: T) -> DmWindow[T]:
        window = DmWindow(first)
        if self._stopping:
            window.flush()
        self._open[key] = window
        return window

    def close(self, key: K, window: DmWindow[T]) -> None:
        """Close `window` if it is still the conversation's; later messages open a new one."""
        if self._open.get(key) is window:
            del self._open[key]

    def flush_all(self) -> None:
        """End every wait now, and skip it for any window opened later: the process is stopping."""
        self._stopping = True
        for window in self._open.values():
            window.flush()
