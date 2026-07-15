"""In-memory sliding-window rate limiter for notebook publishes.

Pure data structure: deque of timestamps per key. Caller passes ``now``
explicitly so tests don't need to monkeypatch the clock — the limiter
itself has no clock dependency.

This is single-process state. The notebook-bot is one Fly Machine today;
when that changes, swap in a Redis-backed implementation. The public
``check_and_record`` signature stays the same — only the storage swaps.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class RateLimiter:
    """Sliding-window rate limiter keyed by an opaque string.

    Default window is one hour; ``max_requests`` caps how many calls fit
    inside that window. ``check_and_record`` either records the call and
    returns True, or returns False without recording.
    """

    max_requests: int
    window: timedelta = timedelta(hours=1)
    _now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _calls: dict[str, deque[datetime]] = field(default_factory=dict[str, deque[datetime]])

    def check_and_record(self, key: str) -> bool:
        """Return True if the call is permitted (and record it). False = deny."""
        if self.max_requests <= 0:
            return True  # disabled — always allow
        now = self._now()
        bucket = self._calls.setdefault(key, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True

    def refund(self, key: str) -> None:
        """Pop the most recent recorded call for ``key``.

        Used when a downstream host call fails — the slot was speculatively
        spent at check time, and the caller's policy is "don't punish the
        principal for the host flapping." If there are no recorded calls
        (already empty, or refund called twice), this is a no-op.
        """
        bucket = self._calls.get(key)
        if bucket:
            bucket.pop()

    def remaining(self, key: str) -> int:
        """How many more calls fit in the current window for ``key``. Diagnostic only."""
        if self.max_requests <= 0:
            return self.max_requests
        bucket = self._calls.get(key)
        if bucket is None:
            return self.max_requests
        cutoff = self._now() - self.window
        live = sum(1 for ts in bucket if ts >= cutoff)
        return max(0, self.max_requests - live)
