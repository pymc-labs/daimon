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
    #: Most keys held at once. Past it, expired buckets go first; if none are left a
    #: new key is denied, so a flood of distinct keys cannot grow the dict without bound.
    max_keys: int = 10_000
    _now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _calls: dict[str, deque[datetime]] = field(default_factory=dict[str, deque[datetime]])

    def check_and_record(self, key: str) -> bool:
        """Return True if the call is permitted (and record it). False = deny."""
        if self.max_requests <= 0:
            return True  # disabled — always allow
        now = self._now()
        cutoff = now - self.window
        bucket = self._calls.get(key)
        if bucket is None:
            if len(self._calls) >= self.max_keys:
                self._evict_expired(cutoff)
                if len(self._calls) >= self.max_keys:
                    return False
            bucket = self._calls[key] = deque()
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True

    def _evict_expired(self, cutoff: datetime) -> None:
        """Drop every bucket with no call inside the window."""
        for key in [k for k, bucket in self._calls.items() if not bucket or bucket[-1] < cutoff]:
            del self._calls[key]

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
            if not bucket:
                del self._calls[key]

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
