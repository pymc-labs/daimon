"""Process-local pacing for Anthropic Skills API requests.

The SDK retries 429 responses with exponential backoff and honors Retry-After.
Pacing at the HTTP transport also covers those retries and every Skills API
caller that shares the process's Anthropic client.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from time import monotonic

import httpx2
from anthropic import DEFAULT_CONNECTION_LIMITS
from daimon.core.anthropic_spend import spend_limit_response
from daimon.core.runtime_health import record_anthropic_response


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


class SkillsRateLimitedTransport(httpx2.AsyncBaseTransport):
    """Pace Skills requests and stop SDK retries at the monthly spend cap."""

    def __init__(
        self, requests_per_minute: int, *, inner: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._pacer = _process_pacer(requests_per_minute)
        self._inner = (
            inner
            if inner is not None
            else httpx2.AsyncHTTPTransport(limits=DEFAULT_CONNECTION_LIMITS)
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/v1/skills"):
            await self._pacer.wait()
        response = await self._inner.handle_async_request(request)
        record_anthropic_response(request, response)
        if response.status_code == 429:
            await response.aread()
            if spend_limit_response(response) == "org_cap":
                response.headers["x-should-retry"] = "false"
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
