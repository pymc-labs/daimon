"""A source's request budget: the most upstream calls a sync may make per minute."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class RateBudget:
    """A token bucket, one per source. A connector `await`s `take()` before each upstream call
    so that sources sharing a credential, or one busy source, stay inside the provider's limit.
    `per_minute` of 0 or less is unlimited."""

    def __init__(
        self, per_minute: float, clock: Callable[[], float] = time.monotonic, burst: float = 1.0
    ) -> None:
        self._rate = per_minute / 60.0
        self._cap = max(burst, 1.0)
        self._tokens = self._cap
        self._clock = clock
        self._at = clock()
        self._lock = asyncio.Lock()

    async def take(self, n: float = 1.0) -> None:
        """Wait until `n` calls fit in the budget, then spend them."""
        if self._rate <= 0:
            return
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._cap, self._tokens + (now - self._at) * self._rate)
                self._at = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self._rate)
