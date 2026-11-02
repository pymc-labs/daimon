"""`scheduler.tick_completed` shows the tick loop is advancing, at most once a minute."""

from __future__ import annotations

import asyncio
import unittest.mock

import pytest
from daimon.adapters.scheduler.main import (
    _run_loops,  # pyright: ignore[reportPrivateUsage]  # the loop that wraps the tick
    _tick_progress,  # pyright: ignore[reportPrivateUsage]  # injectable clock for the throttle
)
from structlog.testing import capture_logs


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _completed(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "scheduler.tick_completed"]


async def test_first_tick_logs_its_duration_then_at_most_one_line_a_minute() -> None:
    clock = _Clock()
    durations = iter([2.5, 1.0, 1.0, 0.25])

    async def tick() -> None:
        clock.now += next(durations)

    step = _tick_progress(tick, clock=clock)
    with capture_logs() as logs:
        await step()  # first tick: logged, 2.5 s
        clock.now += 10
        await step()  # 10 s later: throttled
        clock.now += 40
        await step()  # still inside the minute: throttled
        clock.now += 30
        await step()  # a minute since the first line: logged

    lines = _completed(logs)
    assert [(line["duration_ms"], line["log_level"]) for line in lines] == [
        (2500, "info"),
        (250, "info"),
    ]
    assert set(lines[0]) == {"event", "log_level", "duration_ms"}


async def test_a_tick_that_raises_logs_nothing() -> None:
    clock = _Clock()
    outcomes = [RuntimeError("tick failed"), None]

    async def tick() -> None:
        failure = outcomes.pop(0)
        if failure is not None:
            raise failure

    step = _tick_progress(tick, clock=clock)
    with capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await step()
        assert _completed(logs) == []
        await step()  # the failure did not use up the first-tick line

    assert len(_completed(logs)) == 1


async def test_the_tick_loop_reports_progress_but_the_other_loops_do_not() -> None:
    stop = asyncio.Event()
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            stop.set()

    with capture_logs() as logs:
        async with asyncio.timeout(5):
            await _run_loops(
                tick=tick,
                usage_sweep=unittest.mock.AsyncMock(),
                privacy_sweep=unittest.mock.AsyncMock(),
                compute_sweep=unittest.mock.AsyncMock(),
                compute_task_poll=unittest.mock.AsyncMock(),
                interval_s=0.01,
                stop_event=stop,
            )

    assert ticks == 3
    assert len(_completed(logs)) == 1, "three fast ticks are one line"
