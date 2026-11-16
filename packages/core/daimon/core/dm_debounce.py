"""Hold a burst of direct messages, so one turn answers all of them.

The first message of a burst opens it; each later message from the same person in the
same conversation joins it, also while a turn runs. A turn starts once nothing new has
arrived for the configured wait, or ten seconds after the first waiting message; what
arrives during that turn is answered by the next one. A held message is marked until its
turn ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections.abc import Awaitable, Callable, Hashable, Sequence
from time import monotonic

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature
from daimon.core.stores.pending_skill_adds import is_affirmative

MAX_WAIT_S = 10.0
"""The longest a message waits for the rest of its burst."""

type React[T] = Callable[[T, bool], Awaitable[None]]
"""Add (True) or remove (False) the mark that says a message is held."""


async def debounce_seconds(
    sessions: Sessions, settings: Settings | None, tenant_id: uuid.UUID, platform: str
) -> float:
    """The wait before a direct message starts a turn; 0 answers each message at once."""
    found = await place_feature(
        sessions, settings, FeaturePlace.of(tenant_id, platform, None), "dm_debounce_seconds"
    )
    return float(found.value)


def split_approvals[T](items: Sequence[T], text_of: Callable[[T], str]) -> list[list[T]]:
    """One turn per run of ordinary messages; an approval reply ("yes") stands alone."""
    groups: list[list[T]] = []
    run: list[T] = []
    for item in items:
        if is_affirmative(text_of(item)):
            if run:
                groups.append(run)
                run = []
            groups.append([item])
        else:
            run.append(item)
    if run:
        groups.append(run)
    return groups


class DmBurst[T]:
    """The unanswered messages of one conversation, in the order they arrived."""

    def __init__(self, first: T, text_of: Callable[[T], str]) -> None:
        self.unanswered: list[T] = [first]
        self.closed = False
        self._text_of = text_of
        self._marks: list[tuple[T, React[T]]] = []
        self._first = self._last = monotonic()
        self._wake = asyncio.Event()
        self._flush = is_affirmative(text_of(first))
        self._stopped = False

    def add(self, item: T) -> None:
        """Join the burst. Never awaits, so a message cannot slip between a close and a join.

        An approval reply ends the wait at once: it answers a question already asked.
        """
        self._last = monotonic()
        self._first = min(self._first, self._last)
        self.unanswered.append(item)
        self._flush = self._flush or is_affirmative(self._text_of(item))
        self._wake.set()

    def stop(self) -> None:
        """Skip every wait from now on: the process is stopping."""
        self._stopped = True
        self._wake.set()

    async def mark(self, item: T, react: React[T]) -> None:
        """Mark a held message, and unmark it at once if its turn already ended."""
        await react(item, True)
        if any(held is item for held in self.unanswered):
            self._marks.append((item, react))
        else:
            await react(item, False)

    async def answer(
        self,
        seconds: float,
        react: React[T],
        answer_group: Callable[[list[T]], Awaitable[None]],
    ) -> None:
        """Answer every message, one turn per group, until nothing is left waiting.

        The first message is marked only when it waits. A group's own failures are its
        handler's to report; anything it raises ends the burst with the rest unanswered.
        """
        if seconds > 0 and not (self._flush or self._stopped):
            await self.mark(self.unanswered[0], react)
        while batch := await self._take(seconds):
            for group in split_approvals(batch, self._text_of):
                await answer_group(group)
                del self.unanswered[: len(group)]
                await self._unmark()

    async def close(self) -> list[T]:
        """Close the burst and unmark it; the messages it never answered."""
        self.closed = True
        left, self.unanswered = self.unanswered, []
        await self._unmark()
        return left

    async def _take(self, seconds: float) -> list[T]:
        """Wait out the burst; the messages waiting, or none, which closes it.

        The cap counts from the first waiting message, so the lookups and the turn before
        count towards it. Nothing awaits between the last check and the close.
        """
        while self.unanswered and seconds > 0 and not (self._flush or self._stopped):
            remaining = min(self._last + seconds, self._first + MAX_WAIT_S) - monotonic()
            if remaining <= 0:
                break
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), remaining)
        batch = list(self.unanswered)
        self.closed = not batch
        self._first, self._flush = math.inf, False
        return batch

    async def _unmark(self) -> None:
        waiting = {id(item) for item in self.unanswered}
        done = [mark for mark in self._marks if id(mark[0]) not in waiting]
        self._marks = [mark for mark in self._marks if id(mark[0]) in waiting]
        for item, react in done:
            await react(item, False)


class DmBursts[K: Hashable, T]:
    """The open burst of each conversation."""

    def __init__(self, text_of: Callable[[T], str]) -> None:
        self._open: dict[K, DmBurst[T]] = {}
        self._text_of = text_of
        self._stopping = False

    def join(self, key: K, item: T) -> DmBurst[T] | None:
        """Add `item` to the conversation's open burst; None when it has none."""
        burst = self._open.get(key)
        if burst is None or burst.closed:
            return None
        burst.add(item)
        return burst

    def open(self, key: K, first: T) -> DmBurst[T]:
        burst = DmBurst(first, self._text_of)
        if self._stopping:
            burst.stop()
        self._open[key] = burst
        return burst

    async def end(self, key: K, burst: DmBurst[T]) -> list[T]:
        """Close `burst`, so later messages open a new one; the messages it never answered."""
        if self._open.get(key) is burst:
            del self._open[key]
        return await burst.close()

    def stop(self) -> None:
        """Skip every wait, now and in bursts opened later: the process is stopping."""
        self._stopping = True
        for burst in self._open.values():
            burst.stop()
