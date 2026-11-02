"""Shared pieces for the HTTP fakes: a scripted response body and the turn's progress."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Literal

import httpx

from .script import Conn, Script


class ScriptedBody(httpx.AsyncByteStream):
    """A streamed body that sends `chunks`, then ends the way the connection says."""

    def __init__(self, chunks: list[bytes], end: Conn, progress: Progress) -> None:
        self.chunks, self.end, self.progress = chunks, end, progress
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        await asyncio.sleep(0)
        if self.end.end != "terminal":
            self.progress.quiet = True
        if self.end.end in ("terminal", "eof"):
            return
        request = httpx.Request("GET", "https://contract.invalid/stream")
        if self.end.end == "read_error":
            raise httpx.ReadError("connection reset mid-turn", request=request)
        if self.end.end == "protocol_error":
            raise httpx.RemoteProtocolError("peer closed mid-chunk", request=request)
        # hold: open and silent. The real transport's read timeout is the only exit.
        await asyncio.sleep(_HOLD_S)
        raise httpx.ReadTimeout("no bytes within the read timeout", request=request)

    async def aclose(self) -> None:
        self.closed = True


_HOLD_S = 0.3
"""How long a held connection stays silent before its read times out. MockTransport
has no socket clock, so the fake raises the timeout the real transport would."""


class Progress:
    """How far the service has got through one scripted turn.

    `produce(n)` makes `n` more answer records; `finish()` makes the rest and the
    turn's end. Records are built once, by `make(index)`, so every read of the
    history returns the same identity and content for a record.
    """

    def __init__(self, script: Script, make: Callable[[int], dict[str, object]]) -> None:
        self.script, self.make = script, make
        self.records: list[dict[str, object]] = []
        self.ended = False
        self.opens = 0
        self.quiet = False
        """A connection has ended without the turn's end: reads now may fail."""
        self.phase: Literal["status", "replay"] | None = None
        """The host read in flight, set by `drive` around `io.status` and `io.replay`."""
        self.polled = False
        """A polled wire: its own poll is the read after a quiet stream."""
        self.status_failed = 0
        self.replay_failed = 0
        self.interrupts = 0
        self.order: list[str] = []
        """`interrupt` and `finalized`, in the order they happened."""

    def produce(self, count: int) -> list[dict[str, object]]:
        start = len(self.records)
        stop = min(self.script.pieces, start + count)
        self.records.extend(self.make(i) for i in range(start, stop))
        return self.records[start:]

    def finish(self) -> list[dict[str, object]]:
        if self.script.stuck:
            return []
        start = len(self.records)
        self.produce(self.script.pieces)
        if not self.ended:
            self.ended = True
            self.records.append(self.make(-1))
        return self.records[start:]

    def connect(self) -> tuple[Conn, list[dict[str, object]]]:
        """The next connection and the records it delivers live."""
        conn = self.script.conn(self.opens)
        self.opens += 1
        if conn.end == "terminal":
            return conn, self.finish()
        return conn, self.produce(conn.deliver)

    def read_fails(self) -> bool:
        """Whether this wire read, made inside the host's status read or replay
        after a quiet stream, is one the script fails. Counted per operation."""
        if not self.quiet:
            return False
        phase = "status" if self.phase is None and self.polled else self.phase
        if phase == "status" and self.status_failed < self.script.status_failures:
            self.status_failed += 1
            return True
        if phase == "replay" and self.replay_failed < self.script.replay_failures:
            self.replay_failed += 1
            return True
        return False

    def interrupted(self) -> None:
        self.interrupts += 1
        self.order.append("interrupt")

    def facts(self) -> dict[str, object]:
        return {
            "opens": self.opens,
            "interrupts": self.interrupts,
            "status_failed": self.status_failed,
            "replay_failed": self.replay_failed,
            "order": self.order,
        }


def unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": {"type": "overloaded", "message": "try again"}})
