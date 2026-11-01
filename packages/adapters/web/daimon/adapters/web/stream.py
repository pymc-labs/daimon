"""A running turn's events, kept so a browser can re-attach after the last one it saw.

An event's id is `<turn>:<n>`: the turn's own epoch and its position. A browser that
comes back with another turn's id (a new turn, a restart) is sent a snapshot.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi.responses import StreamingResponse

Event = dict[str, Any]

#: Events kept per turn; a browser further behind gets a snapshot instead.
KEEP_EVENTS = 4096
#: How long a finished turn's events stay readable.
KEEP_CLOSED_S = 300.0
#: A comment line this often keeps proxies from closing an idle stream.
PING_S = 15.0


#: `TurnStream.position` of an id that is not from this turn.
OTHER_TURN = -1


class TurnStream:
    """One turn's events with increasing ids. A `done` event ends a segment: the reply
    so far is saved, and the turn either ended or waits on a card."""

    def __init__(self, message_id: uuid.UUID) -> None:
        self.message_id = message_id  # the user message that started the turn
        self.epoch = uuid.uuid4().hex[:12]
        self.closed_at: float | None = None
        self.waiting = False  # a card is open: nothing more until it is answered
        self.reply_id: uuid.UUID | None = None  # the reply the live events build
        # The live reply as events, for a reader whose `after` was dropped.
        self.snapshot: Callable[[], list[Event]] = list
        self._events: deque[tuple[int, Event]] = deque(maxlen=KEEP_EVENTS)
        self._last = 0
        self._changed = asyncio.Condition()

    @property
    def last_id(self) -> int:
        return self._last

    def event_id(self, position: int) -> str:
        return f"{self.epoch}:{position}"

    def position(self, event_id: str | None) -> int:
        """Where a browser that last saw `event_id` stands: 0 with none, `OTHER_TURN` for
        an id this turn did not issue."""
        if not event_id:
            return 0
        epoch, _, seen = event_id.partition(":")
        return int(seen) if epoch == self.epoch and seen.isdigit() else OTHER_TURN

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    async def publish(self, event: Event) -> int:
        async with self._changed:
            self._last += 1
            self._events.append((self._last, event))
            self._changed.notify_all()
            return self._last

    async def close(self) -> None:
        async with self._changed:
            self.closed_at = time.monotonic()
            # The saved reply and the buffered `done` serve a late reader; the turn's
            # state the snapshot reads is no longer needed.
            self.snapshot = list
            self._changed.notify_all()

    def _has_news(self, after: int) -> bool:
        return self._last > after or self.closed

    async def segment(self, after: int) -> AsyncIterator[tuple[int, Event]]:
        """Events after `after` up to and including the next `done`, or until closed."""
        if self._events and after < self._events[0][0] - 1:
            # Dropped from the buffer: a buffered `done` holds the whole reply;
            # otherwise the reply as it stands, then live events.
            done = next(((i, e) for i, e in self._events if e["type"] == "done"), None)
            if done is not None:
                yield done
                return
            for event in self.snapshot():
                yield self._last, event
            after = self._last
        after = max(after, 0)
        while True:
            async with self._changed:
                await self._changed.wait_for(functools.partial(self._has_news, after))
                fresh = [(i, e) for i, e in self._events if i > after]
                closed = self.closed
            for event_id, event in fresh:
                yield event_id, event
                if event["type"] == "done":
                    return
                after = event_id
            if closed and not fresh:
                return


class StreamRegistry:
    """The turn stream of each web thread; a finished one is dropped after a while."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._streams: dict[uuid.UUID, TurnStream] = {}
        self._clock = clock

    def get(self, thread_id: uuid.UUID) -> TurnStream | None:
        stream = self._streams.get(thread_id)
        closed_at = stream.closed_at if stream is not None else None
        if closed_at is not None and self._clock() - closed_at > KEEP_CLOSED_S:
            del self._streams[thread_id]
            return None
        return stream

    def running(self, thread_id: uuid.UUID) -> TurnStream | None:
        stream = self.get(thread_id)
        return stream if stream is not None and not stream.closed else None

    def open(self, thread_id: uuid.UUID, message_id: uuid.UUID) -> TurnStream:
        if self.running(thread_id) is not None:
            raise RuntimeError("a turn is already running in this thread")
        stream = self._streams[thread_id] = TurnStream(message_id)
        return stream

    def close_later(self, thread_id: uuid.UUID, stream: TurnStream) -> None:
        """Drop a closed `stream` after `KEEP_CLOSED_S`, though nobody asks for it."""

        def drop() -> None:
            if self._streams.get(thread_id) is stream:
                del self._streams[thread_id]

        asyncio.get_running_loop().call_later(KEEP_CLOSED_S, drop)


def _frame(event_id: str, event: Event) -> str:
    return f"id: {event_id}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


async def _with_pings(stream: TurnStream, after: int) -> AsyncIterator[str]:
    events = stream.segment(after)
    pending = asyncio.ensure_future(anext(events))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=PING_S)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                event_id, event = pending.result()
            except StopAsyncIteration:
                return
            yield _frame(stream.event_id(event_id), event)
            pending = asyncio.ensure_future(anext(events))
    finally:
        pending.cancel()


def sse_response(stream: TurnStream, after: int = 0) -> StreamingResponse:
    """The stream's events after position `after`, up to the next `done`."""
    return StreamingResponse(
        _with_pings(stream, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
