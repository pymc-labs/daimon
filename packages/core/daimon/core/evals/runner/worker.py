"""The eval worker loop: claim items from the queue and run up to `concurrency` at once.
Several workers may share the queue; an item whose worker is lost is tried once more."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

import structlog
from daimon.core.evals.format import RunItem
from daimon.core.evals.runner.item import ItemDeps, close_lost, run_item
from daimon.core.stores import eval_runs

log = structlog.get_logger()

STALE_AFTER: Final = timedelta(minutes=5)
"""A running item whose worker has not checked in for this long is taken as lost."""


async def run_next(deps: ItemDeps) -> RunItem | None | bool:
    """Claim and run one item: its result (None: stopped by its run or cap), False if none."""
    now = deps.clock()
    async with deps.sessions.begin() as session:
        lost = await eval_runs.fail_lost_items(session, stale_before=now - STALE_AFTER)
    await close_lost(deps.sessions, lost, deps.clock)
    async with deps.sessions.begin() as session:
        item = await eval_runs.claim_item(session, now=now, stale_before=now - STALE_AFTER)
    if item is None:
        return False
    return await run_item(deps, item)


async def work(
    deps: ItemDeps, *, concurrency: int, stop: asyncio.Event, idle_s: float = 2.0
) -> None:
    """Run items until `stop`; running items finish first."""
    running: set[asyncio.Task[RunItem | None | bool]] = set()
    while not stop.is_set():
        if len(running) >= concurrency:
            _, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            continue
        task = asyncio.create_task(run_next(deps))
        running.add(task)
        done, running = await asyncio.wait(running, timeout=0.1)
        if any(t.result() is False for t in done if not t.exception()):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), idle_s)
        for t in done:
            if t.exception() is not None:
                log.error("eval_worker.claim_failed", error=type(t.exception()).__name__)
    if running:
        await asyncio.wait(running)
