"""Waiting for the work an event started, as a test drives a real app from outside."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def settled(timeout_s: float = 20.0) -> AsyncIterator[None]:
    """On exit, wait until every task started inside the block is done."""
    before = asyncio.all_tasks()
    yield
    busy: list[asyncio.Task[object]] = []
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        busy = [
            t
            for t in asyncio.all_tasks() - before
            if t is not asyncio.current_task() and not t.done()
        ]
        if not busy:
            return
    raise AssertionError(f"still busy after {timeout_s}s: {[t.get_name() for t in busy]}")
