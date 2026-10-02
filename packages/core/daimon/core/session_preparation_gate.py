"""Reserve pooled connection headroom before advisory-lock transactions."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from daimon.core.session_fence_retry import fence_acquisition
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import Pool, QueuePool

_waiting = 0
_active = 0
_pool_gates: weakref.WeakKeyDictionary[Pool, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_preparation_pool_gates: weakref.WeakKeyDictionary[Pool, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def pool_headroom(
    factory: async_sessionmaker[AsyncSession], *, preparation: bool = False
) -> AsyncIterator[None]:
    """Reserve separate pre-checkout capacity for preparation and mutation.

    Each holder needs its advisory-lock connection plus at most one nested
    short recheck connection. Split half the pool's total capacity between
    preparations and mutations, reserving mutation slots even during MA I/O.
    At the default 5+10 this admits two preparations and five mutations:
    2 * (2 + 5) = 14 connections. Pools smaller than four connections can
    support only one holder, so must share a gate to preserve nested headroom.
    Production build_engine rejects total capacity below four at startup:
    a long preparation must leave a separate mutation slot and nested headroom.
    The shared fallback supports externally constructed test engines only.

    Key by pool because runtimes and factories can share an engine. Unbounded
    overflow conservatively uses the persistent size for the budget. NullPool,
    unbounded size-zero pools and connection-bound factories need no gate.
    Callers must reuse their lock connection rather than re-enter this gate.
    """
    bind = factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine) or not isinstance(bind.pool, QueuePool):
        yield
        return
    pool = bind.pool
    if pool.size() == 0:
        yield
        return
    gate = _pool_gates.get(pool)
    if gate is None:
        # QueuePool exposes no public accessor for its configured overflow limit.
        overflow = pool._max_overflow  # pyright: ignore[reportPrivateUsage]
        capacity = pool.size() + max(0, overflow)
        holders = capacity // 2
        if holders == 0:
            raise ValueError("Session fences require capacity for a lock and a nested recheck")
        if holders == 1:
            gate = preparation_gate = asyncio.Semaphore(1)
        else:
            preparations = min(max(1, pool.size() // 2), holders - 1)
            preparation_gate = asyncio.Semaphore(preparations)
            gate = asyncio.Semaphore(holders - preparations)
        _pool_gates[pool] = gate
        _preparation_pool_gates[pool] = preparation_gate
    if preparation:
        gate = _preparation_pool_gates[pool]
    async with fence_acquisition():
        await gate.acquire()
    try:
        yield
    finally:
        gate.release()


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
            async with fence_acquisition():
                await self._semaphore.acquire()
        finally:
            _waiting -= 1
        _active += 1
        try:
            yield
        finally:
            _active -= 1
            self._semaphore.release()
