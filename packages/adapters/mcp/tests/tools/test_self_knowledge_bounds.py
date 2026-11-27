"""`self_knowledge` reads run a few at a time: a search walks thousands of files."""

from __future__ import annotations

import asyncio

from daimon.adapters.mcp.tools.self_knowledge import MAX_PARALLEL, bounded


async def test_reads_in_flight_never_exceed_the_bound() -> None:
    slots = asyncio.Semaphore(MAX_PARALLEL)
    running = peak = 0

    async def read() -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return 1

    done = await asyncio.gather(*(bounded(slots, read) for _ in range(8)))
    assert sum(done) == 8, "every read still completes"
    assert peak == MAX_PARALLEL, f"{peak} reads ran at once, the bound is {MAX_PARALLEL}"
