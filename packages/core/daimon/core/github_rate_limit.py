"""Shared classification and retry deadlines for GitHub rate-limit responses."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx


class GitHubRateLimitError(Exception):
    """GitHub rate-limit response with the earliest safe retry time."""

    def __init__(self, retry_after: datetime) -> None:
        super().__init__("GitHub rate limit exceeded")
        self.retry_after = retry_after


def rate_limit_retry_after(response: httpx.Response) -> datetime:
    """Return the latest provider deadline or GitHub's one-minute fallback."""
    now = datetime.now(UTC)
    deadlines: list[datetime] = []
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            seconds = float(retry_after)
            if math.isfinite(seconds):
                deadlines.append(now + timedelta(seconds=max(0.0, seconds)))
        except (ValueError, OverflowError):
            pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                reset_at = datetime.fromtimestamp(float(reset), UTC)
            except (ValueError, OverflowError, OSError):
                pass
            else:
                deadlines.append(reset_at)
    return max(deadlines) if deadlines else now + timedelta(seconds=60)


async def is_rate_limit_response(response: httpx.Response) -> bool:
    """Recognize explicit 429s and GitHub's rate-limit-shaped 403s."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("retry-after") is not None:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    await response.aread()
    return b"rate limit" in response.content.lower()
