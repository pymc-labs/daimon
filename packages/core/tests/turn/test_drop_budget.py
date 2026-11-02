"""N1: the dropped-connection budget resets when a reconnect delivers new events."""

from types import SimpleNamespace
from typing import cast

from daimon.core.turn import driver
from tenacity import RetryCallState


def _state(attempt: int) -> RetryCallState:
    return cast(RetryCallState, SimpleNamespace(attempt_number=attempt))


def test_two_drops_with_no_new_event_between_them_stop() -> None:
    delivered: set[str] = set()
    stop = driver._drop_budget(delivered, "s")  # pyright: ignore[reportPrivateUsage]
    assert not stop(_state(1))
    assert stop(_state(2))


def test_a_reconnect_that_delivers_new_events_starts_a_fresh_budget() -> None:
    delivered: set[str] = set()
    stop = driver._drop_budget(delivered, "s")  # pyright: ignore[reportPrivateUsage]
    assert not stop(_state(1))
    delivered.add("e1")
    assert not stop(_state(2))
    delivered.add("e2")
    assert not stop(_state(3))
    assert stop(_state(4))


def test_progress_never_lifts_the_hard_cap_per_generation() -> None:
    delivered: set[str] = set()
    stop = driver._drop_budget(delivered, "s")  # pyright: ignore[reportPrivateUsage]
    cap = driver._DROP_ATTEMPTS_PER_GENERATION  # pyright: ignore[reportPrivateUsage]
    for attempt in range(1, cap):
        delivered.add(f"e{attempt}")
        assert not stop(_state(attempt))
    delivered.add("last")
    assert stop(_state(cap))
