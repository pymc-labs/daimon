"""`turn.would_stall`: silence measured per step, never a change to how a turn ends."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import cast

import pytest
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
)
from daimon.core.turn import driver
from daimon.core.turn.io import LegacyTurnIO, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import (
    BlockForever,
    DelayThenYield,
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)
from mux.contracts.receipts import StopObservation

from .conftest import make_agent_message, make_end_turn, make_status_idle, make_tool_use

_EXEMPT = BillingExempt(reason="cli-operator-run")


@pytest.fixture(autouse=True)
def fast_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_WOULD_STALL_STEPS_S", (0.05, 0.1))
    monkeypatch.setattr(driver, "_WOULD_STALL_TICK_S", 0.01)


async def _run_until_stopped(fake: FakeAnthropic, *, after_s: float) -> TurnState:
    cancel = asyncio.Event()

    async def stop_later() -> None:
        await asyncio.sleep(after_s)
        cancel.set()

    stopper = asyncio.create_task(stop_later())
    try:
        async with asyncio.timeout(10):
            return await driver.run_turn(
                anthropic=cast(AsyncAnthropic, fake),
                session_id="sess_1",
                user_message="hi",
                lifecycle=RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=5.0,
                billing=_EXEMPT,
            )
    finally:
        stopper.cancel()


def _would_stall(logs: list[structlog.typing.EventDict]) -> list[int]:
    return [entry["step_s"] for entry in logs if entry["event"] == "turn.would_stall"]


async def test_each_silence_step_is_logged_once_and_the_ending_is_unchanged() -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_agent_message(event_id="sevt_1", text="thinking")), BlockForever()],
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]

    with structlog.testing.capture_logs() as logs:
        final = await _run_until_stopped(fake, after_s=0.4)

    assert _would_stall(logs) == [0, 0], "steps of 0.05 s and 0.1 s log as whole seconds, once each"
    assert final.termination != TerminationReason.CEILING, "measurement never ends a turn"
    entry = next(e for e in logs if e["event"] == "turn.would_stall")
    assert {"session_id", "silent_s", "frame_silent_s", "tool_open", "last_status"} <= set(entry)
    assert entry["tool_open"] is False


async def test_silence_with_a_tool_call_open_is_logged_as_such() -> None:
    """A long tool run is quiet by design: every step says a tool call was open."""
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_tool_use(event_id="tu_1", name="bash")), BlockForever()],
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]

    with structlog.testing.capture_logs() as logs:
        await _run_until_stopped(fake, after_s=0.4)

    stalls = [entry for entry in logs if entry["event"] == "turn.would_stall"]
    assert len(stalls) == 2
    assert all(entry["tool_open"] is True for entry in stalls)


async def test_each_step_is_logged_once_a_turn() -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="a")),
            DelayThenYield(0.15, make_agent_message(event_id="sevt_2", text="b")),
            BlockForever(),
        ],
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]

    with structlog.testing.capture_logs() as logs:
        await _run_until_stopped(fake, after_s=0.4)

    assert len(_would_stall(logs)) == 2, "each step is logged once a turn, however often it recurs"


async def test_an_event_that_lands_during_the_idle_check_is_folded_before_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14: the idle answer may be about the very turn end the stream just delivered."""
    fake = FakeAnthropic()
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    answer = make_agent_message(event_id="answer", text="ok")
    fake.beta.sessions.events.stream_scripts = [[YieldEvent(answer), DelayThenYield(0.08, done)]]
    fake.beta.sessions.events.replay_events = [answer]

    class SlowIdleCheck:
        idle_check_s = 0.05

        def __init__(self) -> None:
            self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")

        async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
            return await self.source.open_stream(read_timeout_s=read_timeout_s)

        async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
            await self.source.send(events)

        async def status(self) -> str:
            await asyncio.sleep(0.06)  # the terminal event lands meanwhile
            return "idle"

        async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
            return await self.source.replay(timeout_s=timeout_s)

        async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
            return await self.source.interrupt(timeout_s=timeout_s)

        async def archive(self) -> None:
            await self.source.archive()

    io = SlowIdleCheck()

    def factory(*args: object, **kwargs: object) -> SlowIdleCheck:
        return io

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = RecordingLifecycle()
    async with asyncio.timeout(10):
        final = await driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
        )

    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(lifecycle.terminal_success) == 1


async def test_the_rendered_line_matches_the_log_metrics_step_extractor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The staging and prod metric reads `"step_s": <digits>` from the JSON line."""
    import json
    import re

    from daimon.core.logging_setup import configure_log_level

    configure_log_level("INFO")
    try:
        silence = driver._Silence()  # pyright: ignore[reportPrivateUsage]
        silence.last_native -= 1.0
        watcher = asyncio.create_task(
            driver._watch_silence(silence, [TurnState()], session_id="sess_1")  # pyright: ignore[reportPrivateUsage]
        )
        await asyncio.sleep(0.2)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    finally:
        structlog.reset_defaults()

    lines = [line for line in capsys.readouterr().out.splitlines() if "turn.would_stall" in line]
    assert lines
    record = json.loads(lines[0])
    assert re.search(r'"step_s": ([0-9]+)', lines[0]) is not None
    assert record["event"] == "turn.would_stall"


async def test_events_landing_during_every_idle_read_cannot_postpone_the_idle_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14 folds the event that landed, then the idle verdict stands."""
    fake = FakeAnthropic()
    chatter = [
        DelayThenYield(0.07, make_agent_message(event_id=f"sevt_{i}", text=".")) for i in range(30)
    ]
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [[*chatter, BlockForever()]]
    fake.beta.sessions.events.replay_events = [done]
    fake.beta.sessions.retrieve_statuses = ["idle"]
    reads: list[str] = []

    class SlowIdleCheck:
        idle_check_s = 0.05

        def __init__(self) -> None:
            self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")

        async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
            return await self.source.open_stream(read_timeout_s=read_timeout_s)

        async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
            await self.source.send(events)

        async def status(self) -> str:
            reads.append("status")
            await asyncio.sleep(0.03)
            return "idle"

        async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
            return await self.source.replay(timeout_s=timeout_s)

        async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
            return await self.source.interrupt(timeout_s=timeout_s)

        async def archive(self) -> None:
            await self.source.archive()

    io = SlowIdleCheck()

    def factory(*args: object, **kwargs: object) -> SlowIdleCheck:
        return io

    monkeypatch.setattr(driver, "turn_io", factory)
    async with asyncio.timeout(3):
        final = await driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
        )

    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(reads) <= 3, "one idle verdict ends the quiet cycle; chatter cannot postpone it"
