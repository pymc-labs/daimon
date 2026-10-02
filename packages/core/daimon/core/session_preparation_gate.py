"""Bound advisory-lock preparations before they check out a DB connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_waiting = 0
_active = 0


def preparation_counts() -> dict[str, int]:
    """Process-local counts for runtime health."""
    return {"waiting": _waiting, "active": _active}


class PreparationGate:
    def __init__(self, limit: int) -> None:
        self._semaphore = asyncio.Semaphore(limit)

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        global _waiting, _active
        _waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            _waiting -= 1
        _active += 1
        try:
            yield
        finally:
            _active -= 1
            self._semaphore.release()
