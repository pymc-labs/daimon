"""Bound advisory-lock preparations before they check out a DB connection."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import Pool, QueuePool

_waiting = 0
_active = 0
_pool_gates: weakref.WeakKeyDictionary[Pool, asyncio.Semaphore] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def pool_headroom(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Share a pre-checkout bound across preparations, recovery and mutations.

    Long advisory-lock transactions use at most half the persistent pool
    (at least one slot). The remaining connections and overflow are available
    for their nested, short tenant-lock/admission transactions. Key by pool,
    rather than runtime or factory: several runtimes can share one engine.
    NullPool has no checkout limit; connection-bound test factories already
    own their connection and cannot reserve it here.
    """
    bind = factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine) or not isinstance(bind.pool, QueuePool):
        yield
        return
    pool = bind.pool
    gate = _pool_gates.get(pool)
    if gate is None:
        gate = asyncio.Semaphore(max(1, pool.size() // 2))
        _pool_gates[pool] = gate
    async with gate:
        yield


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
