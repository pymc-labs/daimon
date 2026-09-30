"""Process-local pacing for Anthropic Skills API requests.

The SDK retries 429 responses with exponential backoff and honors Retry-After.
Pacing at the HTTP transport also covers those retries and every Skills API
caller that shares the process's Anthropic client.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from time import monotonic

import httpx


class _Pacer:
    def __init__(self, requests_per_minute: int) -> None:
        self.interval = 60.0 / requests_per_minute
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
            if delay:
                await asyncio.sleep(delay)


@lru_cache(maxsize=8)
def _process_pacer(requests_per_minute: int) -> _Pacer:
    return _Pacer(requests_per_minute)


class SkillsRateLimitedTransport(httpx.AsyncBaseTransport):
    """Pace every ``/v1/skills`` request, including SDK retry attempts."""

    def __init__(
        self, requests_per_minute: int, *, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._pacer = _process_pacer(requests_per_minute)
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/skills"):
            await self._pacer.wait()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
