"""The opt-in idle status check: a silent stream never hides a turn's end (J4 acb5cd66).

OpenAI's event stream replays nothing and keeps a connection alive with
keep-alive frames, so a turn whose end it missed would wait out its deadline. An
IO with `idle_check_s` makes the driver ask for the session status after that much
silence: still running keeps waiting on the same fetch, a failed check is no
verdict, and idle finishes through the replay. The fake here is the legacy IO over
the fake Managed Agents client, opted in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
)
from daimon.core.turn import driver
from daimon.core.turn.io import LegacyTurnIO, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.testing.turn_fakes import (
    BlockForever,
    DelayThenYield,
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)
from mux.contracts.receipts import StopObservation

from .conftest import make_agent_message, make_end_turn, make_status_idle


class IdleChecked:
    """The legacy IO, opted into the idle status check (as OpenAITurnIO is)."""

    idle_check_s = 0.05

    def __init__(self, fake: FakeAnthropic, *, fail_first: bool = False, slow_s: float = 0):
        self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")
        self.fail_first, self.slow_s = fail_first, slow_s
        self.checks = 0
        self.replays = 0

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return await self.source.open_stream(read_timeout_s=read_timeout_s)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self.source.send(events)

    async def status(self) -> str:
        self.checks += 1
        if self.fail_first and self.checks == 1:
            request = httpx.Request("GET", "https://api.openai.invalid/v1/agents/sessions/sess_1")
            raise anthropic.APIStatusError(
                "rate limited", response=httpx.Response(429, request=request), body=None
            )
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        return await self.source.status()

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        self.replays += 1
        return await self.source.replay(timeout_s=timeout_s)

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return await self.source.interrupt(timeout_s=timeout_s)

    async def archive(self) -> None:
        await self.source.archive()


def _install(monkeypatch: pytest.MonkeyPatch, io: IdleChecked) -> None:
    def factory(*args: object, **kwargs: object) -> IdleChecked:
        return io

    monkeypatch.setattr(driver, "turn_io", factory)


async def _run(fake: FakeAnthropic, lifecycle: RecordingLifecycle) -> TurnState:
    async with asyncio.timeout(10):  # never the turn's deadline
        return await driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="cli-operator-run"),
        )


def _answered_but_silent(fake: FakeAnthropic) -> None:
    """The answer arrives, then only keep-alives: the turn's end lands in the history."""
    answer = make_agent_message(event_id="answer", text="ok")
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [[YieldEvent(answer), BlockForever()]]
    fake.beta.sessions.events.replay_events = [answer, done]


async def test_a_running_session_keeps_waiting_until_it_reports_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    _answered_but_silent(fake)
    # The eventless path re-reads the status before it replays: the last "idle".
    fake.beta.sessions.retrieve_statuses = ["running", "running", "idle", "idle"]
    io = IdleChecked(fake)
    _install(monkeypatch, io)
    lifecycle = RecordingLifecycle()
    final = await _run(fake, lifecycle)
    assert io.checks == 4, "running twice kept it waiting; idle finished it"
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert [block.text for block in final.content if block.kind == "text"] == ["ok"]
    assert len(lifecycle.terminal_success) == 1
    assert fake.beta.sessions.events.stream_calls == 1, "idle finishes without reopening"


async def test_a_failed_status_check_is_no_verdict_the_turn_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    _answered_but_silent(fake)
    fake.beta.sessions.retrieve_statuses = ["idle", "idle"]
    io = IdleChecked(fake, fail_first=True)
    _install(monkeypatch, io)
    lifecycle = RecordingLifecycle()
    final = await _run(fake, lifecycle)
    assert io.checks == 3, "the 429 was logged and the next check found it idle"
    assert final.error is None
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(lifecycle.terminal_success) == 1


async def test_an_event_arriving_during_the_check_is_folded_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch in flight survives the check: its terminal event ends the turn live."""
    fake = FakeAnthropic()
    answer = make_agent_message(event_id="answer", text="ok")
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(answer), DelayThenYield(seconds=0.08, event=done)]
    ]
    fake.beta.sessions.events.replay_events = []
    fake.beta.sessions.retrieve_statuses = ["running", "running"]
    io = IdleChecked(fake, slow_s=0.05)
    _install(monkeypatch, io)
    lifecycle = RecordingLifecycle()
    final = await _run(fake, lifecycle)
    assert io.checks >= 1 and io.replays == 0, "the stream's own terminal ended it"
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert [block.text for block in final.content if block.kind == "text"] == ["ok"]


async def test_an_io_without_the_opt_in_is_never_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Managed Agents: no `idle_check_s`, so a silent stream is not status-checked."""

    class NotOptedIn(IdleChecked):
        idle_check_s = None  # pyright: ignore[reportAssignmentType]

    fake = FakeAnthropic()
    answer = make_agent_message(event_id="answer", text="ok")
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(answer), DelayThenYield(seconds=0.2, event=done)]
    ]
    fake.beta.sessions.retrieve_statuses = []  # any check would fail the fake
    io = NotOptedIn(fake)
    _install(monkeypatch, io)
    final = await _run(fake, RecordingLifecycle())
    assert io.checks == 0 and fake.beta.sessions.retrieve_calls == []
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"


async def test_an_event_during_an_idle_verdict_is_folded_then_the_verdict_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14: an event that lands while the check reads "idle" is folded, not dropped,
    and the idle verdict still finishes the turn through the replay."""
    fake = FakeAnthropic()
    answer = make_agent_message(event_id="answer", text="ok")
    more = make_agent_message(event_id="more", text="also")
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(answer), DelayThenYield(seconds=0.08, event=more), BlockForever()]
    ]
    fake.beta.sessions.events.replay_events = [answer, more, done]
    fake.beta.sessions.retrieve_statuses = ["idle", "idle"]
    io = IdleChecked(fake, slow_s=0.05)
    _install(monkeypatch, io)
    lifecycle = RecordingLifecycle()
    final = await _run(fake, lifecycle)
    assert io.replays == 1, "the idle verdict stood after the folded event"
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert [block.text for block in final.content if block.kind == "text"] == ["okalso"]
    assert "more" in [e.id for e in lifecycle.sse_events]


async def test_a_terminal_event_during_an_idle_verdict_ends_the_turn_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    answer = make_agent_message(event_id="answer", text="ok")
    done = make_status_idle(event_id="done", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(answer), DelayThenYield(seconds=0.08, event=done)]
    ]
    fake.beta.sessions.events.replay_events = []
    fake.beta.sessions.retrieve_statuses = ["idle"]
    io = IdleChecked(fake, slow_s=0.05)
    _install(monkeypatch, io)
    final = await _run(fake, RecordingLifecycle())
    assert io.replays == 0, "the stream's own terminal ended it"
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
