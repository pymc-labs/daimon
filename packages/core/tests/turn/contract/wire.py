"""Shared pieces for the HTTP fakes: a scripted response body and the turn's progress."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

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
        self.status_reads = 0
        self.replay_reads = 0
        self.quiet = False
        """A connection has ended without the turn's end: reads now may fail."""

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

    def status_fails(self) -> bool:
        if not self.quiet:
            return False
        self.status_reads += 1
        return self.status_reads <= self.script.status_failures

    def replay_fails(self) -> bool:
        if not self.quiet:
            return False
        self.replay_reads += 1
        return self.replay_reads <= self.script.replay_failures


def unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": {"type": "overloaded", "message": "try again"}})
