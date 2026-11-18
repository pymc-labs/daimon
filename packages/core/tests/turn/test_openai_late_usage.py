"""Late OpenAI usage re-reads: bounded backoff, one per turn, outside the turn."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from daimon.core.turn.openai_late_usage import DELAYS_S, LateUsage

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Sleeps:
    """Records each wait instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)


def test_delays_when_summed_then_cover_at_least_an_hour() -> None:
    """Usage has been seen to fill in half an hour after the turn ended."""
    assert sum(DELAYS_S) >= 3600, DELAYS_S


async def test_defer_when_a_read_fails_or_lacks_usage_then_retries_up_to_the_bound() -> None:
    """A raised error or a False result waits for the next delay; the bound stops it."""
    sleeps, seen = Sleeps(), list[datetime]()
    late = LateUsage(delays_s=(1.0, 2.0, 4.0), sleep=sleeps, clock=lambda: NOW)

    async def reread(now: datetime) -> bool:
        seen.append(now)
        if len(seen) == 1:
            raise RuntimeError("busy")
        return False

    late.defer("turn", reread)
    await late.wait()
    assert sleeps.waits == [1.0, 2.0, 4.0], sleeps.waits
    assert seen == [NOW] * 3, "each read gets the injected clock"


async def test_defer_when_the_usage_arrives_then_stops_reading() -> None:
    """A True result ends the re-reads before the bound."""
    sleeps, reads = Sleeps(), list[datetime]()
    late = LateUsage(delays_s=(1.0, 2.0, 4.0), sleep=sleeps, clock=lambda: NOW)

    async def reread(now: datetime) -> bool:
        reads.append(now)
        return len(reads) == 2

    late.defer("turn", reread)
    await late.wait()
    assert (len(reads), sleeps.waits) == (2, [1.0, 2.0]), sleeps.waits


async def test_defer_when_called_twice_for_one_turn_then_reads_once_and_never_waits() -> None:
    """A second defer while one runs is dropped; defer itself returns at once."""
    gate, reads = asyncio.Event(), list[str]()

    async def held(delay: float) -> None:
        await gate.wait()

    late = LateUsage(delays_s=(1.0,), sleep=held, clock=lambda: NOW)

    def reread(name: str):
        async def read(now: datetime) -> bool:
            reads.append(name)
            return True

        return read

    late.defer("turn", reread("first"))
    late.defer("turn", reread("second"))
    assert reads == [], "defer does not wait for the read"
    gate.set()
    await late.wait()
    assert reads == ["first"], reads
