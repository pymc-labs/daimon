"""Bot Framework facts shared by the Teams adapter and the MCP server.

Commercial Microsoft 365 only: sovereign clouds sign and serve from other hosts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog

log = structlog.get_logger()

#: Where proactive sends go when no activity supplied a regional service URL.
SERVICE_URL = "https://smba.trafficmanager.net/teams"
# Longer waits give up: the caller's own send timeout would expire first.
_MAX_RETRY_AFTER_S = 10.0
_DEFAULT_RETRY_AFTER_S = 1.0


def throttle_delay(exc: BaseException) -> float | None:
    """Seconds to wait before retrying `exc`, or None unless it is a 429 worth waiting for."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return None
    try:
        delay = max(0.0, float(exc.response.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)))
    except ValueError:
        delay = _DEFAULT_RETRY_AFTER_S
    return delay if delay <= _MAX_RETRY_AFTER_S else None


async def retry_throttled[T](call: Callable[[], Awaitable[T]]) -> T:
    """Run `call`, and once more after `Retry-After` if Teams throttled it.

    A 429 means Teams did not act on the request, so even a post is safe to
    repeat. One retry, as Slack's clients do.
    """
    try:
        return await call()
    except httpx.HTTPStatusError as exc:
        delay = throttle_delay(exc)
        if delay is None:
            raise
        log.warning("teams.throttled", retry_after_s=delay)
        await asyncio.sleep(delay)
        return await call()
