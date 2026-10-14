"""Clock identity is diagnostic; equal times still have one global order."""

import pytest
from daimon.testing.host_clock import ClockSample, HostClock


def test_shared_clock_orders_equal_time_observations_across_boundaries() -> None:
    clock = HostClock(lambda: 2.0)
    source = clock.sample()
    assert clock() == 2.0
    post = clock.last
    terminal = clock.sample()
    assert source.clock_id == post.clock_id == terminal.clock_id
    assert (source.order, post.order, terminal.order) == (1, 2, 3)
    assert source.observed_s == post.observed_s == terminal.observed_s
    assert ClockSample.model_validate_json(terminal.model_dump_json()) == terminal
    assert HostClock(lambda: 2.0).sample().clock_id != terminal.clock_id


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True])
def test_clock_rejects_invalid_time_before_recording(value: float) -> None:
    clock = HostClock(lambda: value)
    with pytest.raises(ValueError):
        clock.sample()
    with pytest.raises(ValueError, match="no observation"):
        _ = clock.last


def test_backwards_clock_preserves_the_last_actual_sample() -> None:
    values = iter([2.0, 1.0, 3.0])
    clock = HostClock(lambda: next(values))
    opening = clock.sample()
    with pytest.raises(ValueError, match="monotonic"):
        clock.sample()
    assert clock.last == opening
    following = clock.sample()
    assert following.order == 2 and following.observed_s == 3.0


def test_nested_executor_clock_restores_context_after_failure() -> None:
    from daimon.testing.host_clock import current_host_clock, use_host_clock

    original = current_host_clock()
    outer, inner = HostClock(lambda: 1.0), HostClock(lambda: 2.0)
    with use_host_clock(outer):
        assert current_host_clock() is outer
        with pytest.raises(RuntimeError, match="offline failure"), use_host_clock(inner):
            assert current_host_clock() is inner
            raise RuntimeError("offline failure")
        assert current_host_clock() is outer
    assert current_host_clock() is original


async def test_parallel_executors_have_independent_clock_domains() -> None:
    import asyncio

    from daimon.testing.host_clock import current_host_clock, use_host_clock

    async def observe(clock: HostClock) -> ClockSample:
        with use_host_clock(clock):
            await asyncio.sleep(0)
            assert current_host_clock() is clock
            return clock.sample()

    first, second = await asyncio.gather(
        observe(HostClock(lambda: 2.0)), observe(HostClock(lambda: 2.0))
    )
    assert first.clock_id != second.clock_id
    assert first.order == second.order == 1
