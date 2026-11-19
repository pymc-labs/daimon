"""Late OpenAI usage re-reads: bounded backoff, one per turn, outside the turn."""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from daimon.core.turn import openai_late_usage
from daimon.core.turn.openai_late_usage import DELAYS_S, LateUsage
from structlog.testing import capture_logs

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


async def test_defer_when_the_pending_cap_is_reached_then_skips_and_logs() -> None:
    """Past the cap a turn is left unbilled, not queued without bound; a freed slot reopens."""
    gate, reads = asyncio.Event(), list[str]()

    async def held(delay: float) -> None:
        await gate.wait()

    late = LateUsage(delays_s=(1.0,), sleep=held, clock=lambda: NOW, max_pending=2)

    def reread(name: str):
        async def read(now: datetime) -> bool:
            reads.append(name)
            return True

        return read

    with capture_logs() as logs:
        for name in ("a", "b", "c"):
            late.defer(name, reread(name))
    skipped = [entry for entry in logs if entry["event"] == "openai_late_usage.skipped"]
    assert [entry["key"] for entry in skipped] == ["c"], logs
    gate.set()
    await late.wait()
    assert sorted(reads) == ["a", "b"], reads
    late.defer("c", reread("c"))
    await late.wait()
    assert "c" in reads, "a finished turn frees its slot"


async def test_defer_when_the_key_finished_then_it_can_be_deferred_again() -> None:
    """A later revision of the same turn schedules a fresh re-read once the first is done."""
    reads = list[str]()
    late = LateUsage(delays_s=(1.0,), sleep=Sleeps(), clock=lambda: NOW)

    def reread(name: str):
        async def read(now: datetime) -> bool:
            reads.append(name)
            return True

        return read

    late.defer("turn", reread("first"))
    await late.wait()
    late.defer("turn", reread("second"))
    await late.wait()
    assert reads == ["first", "second"], reads


#: Reads of a turn's usage that are not a late re-read, and why.
NOT_LATE_READS = {"hosted_io.py": "the turn's own read when it ends, inside the turn"}


def _calling(name: str) -> set[str]:
    core = Path(openai_late_usage.__file__).parents[1]
    return {
        path.name
        for path in core.rglob("*.py")
        if any(
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == name
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }


def test_both_triggers_start_the_one_shared_recheck() -> None:
    """Found from the code: the in-process and scheduler triggers call `reread_turn`,
    the only late read of a turn resource."""
    triggers = _calling("reread_turn")
    assert triggers == {"openai_host.py", "openai_late_usage.py"}, (
        f"the in-process and scheduler triggers must both start reread_turn: {triggers}"
    )
    reads = _calling("reconcile_turn")
    assert reads == {"openai_late_usage.py", *NOT_LATE_READS}, (
        f"a late read outside reread_turn copies the shared check: {reads}"
    )
