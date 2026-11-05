"""Tests for daimon.core.notebooks._rate_limit.RateLimiter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.notebooks._rate_limit import RateLimiter


def test_rate_limiter_allows_calls_under_cap() -> None:
    """First N calls under max_requests succeed."""
    rl = RateLimiter(max_requests=3)
    assert rl.check_and_record("user-a") is True, "1st call should be allowed"
    assert rl.check_and_record("user-a") is True, "2nd call should be allowed"
    assert rl.check_and_record("user-a") is True, "3rd call should be allowed"


def test_rate_limiter_denies_call_over_cap() -> None:
    """The (max_requests + 1)th call within the window is denied."""
    rl = RateLimiter(max_requests=2)
    rl.check_and_record("user-a")
    rl.check_and_record("user-a")
    assert rl.check_and_record("user-a") is False, "3rd call must be denied when max_requests=2"


def test_rate_limiter_independent_keys() -> None:
    """Two principals do not share a quota."""
    rl = RateLimiter(max_requests=1)
    assert rl.check_and_record("user-a") is True, "user-a's first call should pass"
    assert rl.check_and_record("user-b") is True, (
        "user-b's first call must not be blocked by user-a's call"
    )


def test_rate_limiter_window_expiry_allows_new_calls() -> None:
    """Calls outside the window are forgotten, freeing capacity."""
    fake_now = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)]

    def now() -> datetime:
        return fake_now[0]

    rl = RateLimiter(max_requests=2, window=timedelta(hours=1), _now=now)
    assert rl.check_and_record("u") is True
    assert rl.check_and_record("u") is True
    assert rl.check_and_record("u") is False, "third call same minute should be denied"

    # Advance the clock past the window
    fake_now[0] = datetime(2026, 1, 1, 13, 1, 0, tzinfo=UTC)
    assert rl.check_and_record("u") is True, (
        "after the window expires, the bucket should reset and allow new calls"
    )


def test_rate_limiter_zero_disables() -> None:
    """max_requests=0 disables the limiter (always allow)."""
    rl = RateLimiter(max_requests=0)
    for _ in range(100):
        assert rl.check_and_record("u") is True, (
            "max_requests=0 must mean unlimited — confirms the prod kill-switch"
        )


def test_rate_limiter_remaining_decrements() -> None:
    """remaining() reflects the live count within the window."""
    rl = RateLimiter(max_requests=5)
    assert rl.remaining("u") == 5, "fresh key has full quota"
    rl.check_and_record("u")
    rl.check_and_record("u")
    assert rl.remaining("u") == 3, "two calls consumed → 3 remaining"


def test_rate_limiter_drops_expired_buckets_when_full() -> None:
    """At the key cap, buckets with nothing in the window are evicted to make room."""
    fake_now = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)]
    rl = RateLimiter(
        max_requests=1, window=timedelta(minutes=10), max_keys=2, _now=lambda: fake_now[0]
    )
    assert rl.check_and_record("a") and rl.check_and_record("b"), "two keys fill the cap"

    fake_now[0] += timedelta(minutes=11)

    assert rl.check_and_record("c") is True, "a new key fits once the old ones expired"
    assert set(rl._calls) == {"c"}, "the expired buckets were dropped"  # pyright: ignore[reportPrivateUsage]


def test_rate_limiter_denies_a_new_key_when_full_of_live_ones() -> None:
    """A flood of distinct keys cannot grow the dict past the cap."""
    rl = RateLimiter(max_requests=5, max_keys=2)
    assert rl.check_and_record("a") and rl.check_and_record("b"), "two keys fill the cap"

    assert rl.check_and_record("c") is False, "a third live key is denied, not stored"
    assert rl.check_and_record("a") is True, "a known key still has its allowance"
    assert len(rl._calls) == 2, "the dict did not grow"  # pyright: ignore[reportPrivateUsage]


def test_rate_limiter_refund_drops_an_emptied_bucket() -> None:
    """Refunding the only call leaves no empty bucket behind."""
    rl = RateLimiter(max_requests=1)
    rl.check_and_record("a")

    rl.refund("a")

    assert "a" not in rl._calls, "the empty bucket is gone"  # pyright: ignore[reportPrivateUsage]
