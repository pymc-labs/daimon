"""A turn ends on evidence, and every read that decides it is bounded and finalized.

The shared driver's give-up paths, for every backend: a turn with nothing that
ended it, a reconnect replay that fails or holds only an earlier turn, a status
read that never answers, an IO's own `TurnError`, and Stop before the first read
after a quiet stream.
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
    BetaManagedAgentsTextBlock,
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core.errors import TurnError
from daimon.core.turn import driver
from daimon.core.turn.io import LegacyTurnIO, TurnConnectionLost, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import (
    FakeAnthropic,
    RaiseStreamDrop,
    RecordingLifecycle,
    YieldEvent,
)
from mux.contracts.receipts import StopObservation

from .conftest import make_agent_message, make_end_turn, make_status_idle, make_tool_use

_EXEMPT = BillingExempt(reason="cli-operator-run")


@pytest.fixture(autouse=True)
def no_quiet_read_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_QUIET_READ_RETRY_DELAYS_S", (0.0, 0.0))


def _user(event_id: str, text: str) -> BetaManagedAgentsUserMessageEvent:
    return BetaManagedAgentsUserMessageEvent(
        id=event_id,
        type="user.message",
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
    )


def _unavailable() -> anthropic.APIStatusError:
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1/events")
    return anthropic.APIStatusError(
        "unavailable", response=httpx.Response(503, request=request), body=None
    )


class _Wrapped:
    """The legacy IO over the fake client, with one behaviour swapped in a test."""

    def __init__(self, fake: FakeAnthropic) -> None:
        self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")
        self.sends = 0

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return await self.source.open_stream(read_timeout_s=read_timeout_s)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        self.sends += 1
        await self.source.send(events)

    async def status(self) -> str:
        return await self.source.status()

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        return await self.source.replay(timeout_s=timeout_s)

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return await self.source.interrupt(timeout_s=timeout_s)

    async def archive(self) -> None:
        await self.source.archive()


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeAnthropic,
    io: object,
    *,
    user_message: str = "hi",
    cancel: asyncio.Event | None = None,
) -> tuple[TurnState, RecordingLifecycle]:
    def factory(*args: object, **kwargs: object) -> object:
        return io

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = RecordingLifecycle()
    async with asyncio.timeout(10):
        final = await driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel or asyncio.Event(),
            render_interval_s=0.001,
            interrupt_timeout_s=5.0,
            billing=_EXEMPT,
        )
    return final, lifecycle


async def test_an_idle_session_with_nothing_that_ended_the_turn_is_lost_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[]]  # a clean close, no events
    fake.beta.sessions.events.replay_events = []
    fake.beta.sessions.retrieve_statuses = ["idle"]

    final, lifecycle = await _run(monkeypatch, fake, _Wrapped(fake))

    assert final.termination == TerminationReason.CONNECTION_LOST
    assert lifecycle.terminal_success == []
    assert len(lifecycle.terminal_failures) == 1


async def test_a_reconnect_replay_that_fails_once_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    mid = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseStreamDrop()],
        [YieldEvent(mid), YieldEvent(done)],
    ]
    fake.beta.sessions.events.replay_events = [_user("u1", "hi"), pre]
    fake.beta.sessions.events.list_raises = [_unavailable()]

    final, lifecycle = await _run(monkeypatch, fake, _Wrapped(fake))

    assert final.content == [TextBlock(kind="text", text="hello world")]
    assert len(lifecycle.terminal_success) == 1


async def test_a_status_read_that_never_answers_is_bounded_and_ends_as_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "_STATUS_READ_TIMEOUT_S", 0.01)
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[]]

    class Hanging(_Wrapped):
        reads = 0

        async def status(self) -> str:
            Hanging.reads += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    final, _ = await _run(monkeypatch, fake, Hanging(fake))

    assert Hanging.reads == 3
    assert final.termination == TerminationReason.CONNECTION_LOST


async def test_an_ios_own_turn_error_is_finalized_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[]]
    fake.beta.sessions.retrieve_statuses = ["idle"]

    class DeadlineReplay(_Wrapped):
        replay_may_send = True

        async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
            raise TurnError(kind="upstream", message="replay deadline expired")

    final, lifecycle = await _run(monkeypatch, fake, DeadlineReplay(fake))

    assert final.error is not None and final.error.kind == "upstream"
    assert final.termination == TerminationReason.UPSTREAM
    assert len(lifecycle.terminal_failures) == 1


async def test_stop_before_the_first_read_after_a_quiet_stream_interrupts() -> None:
    """Stop already pressed when the quiet-stream read starts: the provider is
    told to stop (the live interrupt) and nothing is read."""
    cancel = asyncio.Event()
    cancel.set()
    reads: list[str] = []

    async def status() -> str:
        reads.append("status")
        return "idle"

    with pytest.raises(driver._InterruptInConsume):  # pyright: ignore[reportPrivateUsage]
        await driver._read_after_quiet(  # pyright: ignore[reportPrivateUsage]
            status, name="status", session_id="sess_1", cancel=cancel
        )
    assert reads == []


class _UncertainFirstSend(_Wrapped):
    """The first send's outcome is unknown: the reconnect decides what it was."""

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        self.sends += 1
        if self.sends == 1:
            raise TurnConnectionLost("input acknowledgement was lost; its outcome is unknown")
        await self.source.send(events)


@pytest.mark.parametrize("earlier_question", ["the earlier question", "this question"])
async def test_a_reconnect_after_an_uncertain_send_never_adopts_the_previous_turn(
    monkeypatch: pytest.MonkeyPatch, earlier_question: str
) -> None:
    """Even when the earlier question had the very same words."""
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[], []]
    fake.beta.sessions.events.replay_events = [
        _user("u_prev", earlier_question),
        make_agent_message(event_id="a_prev", text="the earlier answer"),
        make_status_idle(event_id="idle_prev", stop_reason=make_end_turn()),
    ]

    final, lifecycle = await _run(
        monkeypatch, fake, _UncertainFirstSend(fake), user_message="this question"
    )

    assert final.termination == TerminationReason.CONNECTION_LOST
    assert final.content == [], "the earlier turn's answer must not become this one's"
    assert lifecycle.terminal_success == []


async def test_a_reconnect_after_an_uncertain_send_on_a_turn_scoped_replay_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    answer = make_agent_message(event_id="a_now", text="this answer")
    done = make_status_idle(event_id="idle_now", stop_reason=make_end_turn())
    fake.beta.sessions.events.stream_scripts = [[], [YieldEvent(answer), YieldEvent(done)]]
    fake.beta.sessions.events.replay_events = [_user("u_now", "this question")]

    class Scoped(_UncertainFirstSend):
        replay_scoped_to_turn = True

    final, lifecycle = await _run(monkeypatch, fake, Scoped(fake), user_message="this question")

    assert final.content == [TextBlock(kind="text", text="this answer")]
    assert len(lifecycle.terminal_success) == 1


async def test_a_turn_that_only_called_a_tool_and_ended_with_no_sign_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narration and a tool call are not an answer: no terminal, no answer, lost."""
    fake = FakeAnthropic()
    narration = make_agent_message(event_id="sevt_1", text="   ")
    call = make_tool_use(event_id="sevt_2", name="bash")
    fake.beta.sessions.events.stream_scripts = [[YieldEvent(narration), YieldEvent(call)]]
    fake.beta.sessions.events.replay_events = [_user("u1", "hi"), narration, call]
    fake.beta.sessions.retrieve_statuses = ["idle"]

    final, lifecycle = await _run(monkeypatch, fake, _Wrapped(fake))

    assert final.termination == TerminationReason.CONNECTION_LOST
    assert lifecycle.terminal_success == []
