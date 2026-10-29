"""Scenario matrix for the eventless-cycle reconnect loop (Class A fix).

Task 1 covers only the injectable read-timeout kwarg threaded to
`events.stream(...)`. Task 2 extends this file with the status-checked
eventless-cycle decision procedure (clean close / read timeout / status
grouping / the untouched connection-error budget).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
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
from daimon.core.turn.io import LegacyTurnIO, TurnStream
from daimon.core.turn.posture import AutoApprove, BillingExempt, RequireApproval
from daimon.core.turn.state import TextBlock
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import (
    DelayThenYield,
    FakeAnthropic,
    RaiseReadError,
    RecordingLifecycle,
    YieldEvent,
    run_turn_on_leg,
)
from mux.contracts.receipts import StopObservation
from mux.errors import ProviderError

from .conftest import make_agent_message, make_end_turn, make_status_idle, make_status_terminated

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)
_EXEMPT = BillingExempt(reason="cli-operator-run")


def _now() -> datetime:
    return _FROZEN_NOW


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


async def test_run_turn_passes_default_stream_read_timeout_to_events_stream() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert len(fa.beta.sessions.events.stream_timeouts) == 1
    timeout = fa.beta.sessions.events.stream_timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 120.0
    assert timeout.connect == 5.0


async def test_run_turn_forwards_explicit_stream_read_timeout_verbatim() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        stream_read_timeout_s=7.5,
    )

    assert len(fa.beta.sessions.events.stream_timeouts) == 1
    timeout = fa.beta.sessions.events.stream_timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 7.5
    assert timeout.connect == 5.0


# --- Task 2: the status-checked eventless-cycle decision procedure --------


async def test_clean_close_while_running_reconnects_and_finalizes_on_replayed_terminal_event() -> (
    None
):
    """A stream that exhausts (StopAsyncIteration) with no terminal event and
    a `running` status must NOT finalize as quiet success -- it reconnects."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close, no terminal event
        [YieldEvent(done)],  # reopened stream delivers the terminal event live
    ]
    fa.beta.sessions.events.replay_events = [pre]
    fa.beta.sessions.retrieve_statuses = ["running"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 2
    assert fa.beta.sessions.retrieve_calls == ["sess_1"]
    assert final.content == [TextBlock(kind="text", text="hello")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert lc.reconnects == ["clean_close"]
    assert len(lc.terminal_success) == 1


async def test_clean_close_while_idle_finalizes_without_reopening_stream() -> None:
    """When the status check says `idle`, the terminal event the driver
    missed lives only in the replay history -- fold it in and finalize
    without ever reopening the stream."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close; `done` never delivered live
    ]
    fa.beta.sessions.events.replay_events = [pre, done]
    fa.beta.sessions.retrieve_statuses = ["idle"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 1, "idle finalize must not reopen the stream"
    assert final.content == [TextBlock(kind="text", text="hello")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1
    assert lc.reconnects == [], "finalize-without-reopen path does not call on_reconnect"


async def test_read_timeout_while_running_reconnects_with_read_timeout_reason() -> None:
    from daimon.testing.turn_fakes import RaiseReadTimeout

    fa = FakeAnthropic()
    done = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [RaiseReadTimeout()],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["running"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 2
    assert lc.reconnects == ["read_timeout"]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"


async def test_read_timeout_while_terminated_finalizes_without_reopening() -> None:
    from daimon.testing.turn_fakes import RaiseReadTimeout

    fa = FakeAnthropic()
    done = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[RaiseReadTimeout()]]
    fa.beta.sessions.events.replay_events = [done]
    fa.beta.sessions.retrieve_statuses = ["terminated"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 1
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1
    assert final.termination is TerminationReason.COMPLETED


async def test_rescheduling_status_behaves_like_running() -> None:
    """`rescheduling` is grouped with `running`, not with `idle`/`terminated`
    -- the session is still active, so the driver reconnects."""
    fa = FakeAnthropic()
    done = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [],  # empty script -> immediate clean close
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["rescheduling"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 2
    assert lc.reconnects == ["clean_close"]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"


async def test_three_consecutive_eventless_cycles_have_no_attempt_cap() -> None:
    """Three consecutive eventless cycles, each reporting `running`, each
    reconnect -- the eventless-cycle loop has no attempt cap unlike the
    bounded 2-attempt connection-error budget. A fourth stream open finally
    delivers the terminal event live.

    (An `idle`/`terminated` status finalizes WITHOUT reopening the stream --
    see the dedicated no-reopen tests above -- so proving "no cap" needs
    every cycle in the chain to report `running`.)
    """
    fa = FakeAnthropic()
    done = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [],  # clean close 1
        [],  # clean close 2
        [],  # clean close 3
        [YieldEvent(done)],  # 4th stream open finally succeeds
    ]
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["running", "running", "running"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 4
    assert lc.reconnects == ["clean_close", "clean_close", "clean_close"]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


async def test_status_check_error_finalizes_as_upstream_turn_error() -> None:
    """`sessions.retrieve` itself erroring must propagate into `_pump`'s
    existing upstream finalizer, not hang or get swallowed."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]  # immediate clean close
    fa.beta.sessions.events.replay_events = []
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1")
    response = httpx.Response(500, request=request)
    fa.beta.sessions.retrieve_raises = anthropic.APIStatusError(
        "boom", response=response, body=None
    )

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert len(lc.terminal_failures) == 1


async def test_replay_after_eventless_cycle_does_not_duplicate_already_folded_content() -> None:
    """`seen_event_ids` is the single dedup authority: a replay that
    re-delivers an event already folded live must not duplicate content."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # live-folds "hello "
        [YieldEvent(pre), YieldEvent(done)],  # MA redelivers pre, then terminal
    ]
    fa.beta.sessions.events.replay_events = [pre]
    fa.beta.sessions.retrieve_statuses = ["running"]

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.content == [TextBlock(kind="text", text="hello ")], (
        "redelivered event must dedup, not duplicate"
    )


async def test_incomplete_replay_does_not_erase_already_folded_content() -> None:
    """A paginated replay that omits an already-folded event must not move
    the state behind the append-only render anchor."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    rendered_before_reconnect = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), DelayThenYield(0.01, rendered_before_reconnect)],
        [YieldEvent(done)],
    ]
    # Inject an incomplete history page. The SDK paginator has no documented
    # snapshot-completeness guarantee, so replay must not erase live state.
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["running"]
    lc = RecordingLifecycle()

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert any(render.content == [TextBlock(kind="text", text="hello ")] for render in lc.renders)
    assert final.content == [TextBlock(kind="text", text="hello world")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"


@pytest.mark.parametrize("tool_confirmation", [RequireApproval(), AutoApprove()])
async def test_terminated_replay_keeps_answer_after_current_turn_idle(
    tool_confirmation: RequireApproval | AutoApprove,
) -> None:
    """A trailing termination event must not make the current idle a prior-turn boundary."""
    fa = FakeAnthropic()
    previous_user = BetaManagedAgentsUserMessageEvent(
        id="user_previous",
        type="user.message",
        content=[BetaManagedAgentsTextBlock(type="text", text="old request")],
    )
    previous_answer = make_agent_message(event_id="answer_previous", text="old answer")
    previous_idle = make_status_idle(event_id="idle_previous", stop_reason=make_end_turn())
    current_user = BetaManagedAgentsUserMessageEvent(
        id="user_current",
        type="user.message",
        content=[BetaManagedAgentsTextBlock(type="text", text="current request")],
    )
    current_answer = make_agent_message(event_id="answer_current", text="current answer")
    current_idle = make_status_idle(event_id="idle_current", stop_reason=make_end_turn())
    terminated = make_status_terminated(event_id="terminated_after_idle")
    fa.beta.sessions.events.stream_scripts = [[]]
    fa.beta.sessions.events.replay_events = [
        previous_user,
        previous_answer,
        previous_idle,
        current_user,
        current_answer,
        current_idle,
        terminated,
    ]
    fa.beta.sessions.retrieve_statuses = ["terminated"]

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="current request",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=tool_confirmation,
    )

    assert final.content == [TextBlock(kind="text", text="current answer")]
    assert final.error is None, "the terminal idle ended this turn before session termination"
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert final.termination is TerminationReason.COMPLETED, (
        "a later termination does not relabel a turn that already ended"
    )


async def test_eventless_cycle_closes_the_abandoned_stream() -> None:
    """The driver must not leak the underlying connection of a stream it
    abandons on an eventless cycle."""
    fa = FakeAnthropic()
    done = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [],  # clean close
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["running"]

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.streams[0].closed is True


async def test_connection_error_regression_still_exhausts_at_two_attempts() -> None:
    """Regression pin: the eventless-cycle loop must not widen the
    error-path budget. Two consecutive `APIConnectionError`s still exhaust
    tenacity's bounded 2-attempt retry and finalize as `connection_lost`,
    tagged `connection_dropped` -- never touching the status check."""
    from daimon.testing.turn_fakes import RaiseConnection

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [RaiseConnection()],
        [RaiseConnection()],
    ]
    fa.beta.sessions.events.replay_events = []

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is not None
    assert final.error.kind == "connection_lost"
    assert lc.reconnects == ["connection_dropped"]
    assert fa.beta.sessions.events.stream_calls == 2
    assert fa.beta.sessions.retrieve_calls == [], (
        "connection-error budget must never consult the status check"
    )


# --- The reads after a quiet stream are retried before a turn is lost -----


def _dropped() -> anthropic.APIConnectionError:
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1")
    return anthropic.APIConnectionError(request=request)


@pytest.fixture
def no_quiet_read_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.core.turn import driver

    monkeypatch.setattr(driver, "_QUIET_READ_RETRY_DELAYS_S", (0.0, 0.0))


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_a_dropped_status_read_after_a_clean_close_is_retried_not_lost() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(pre)], [YieldEvent(done)]]
    fa.beta.sessions.events.replay_events = [pre]
    fa.beta.sessions.retrieve_statuses = [_dropped(), "running"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.retrieve_calls == ["sess_1", "sess_1"]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1
    assert not lc.terminal_failures


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_a_dropped_replay_after_a_quiet_idle_stream_is_retried_not_lost() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(pre)]]
    fa.beta.sessions.events.replay_events = [pre, done]
    fa.beta.sessions.events.list_raises = [_dropped()]
    fa.beta.sessions.retrieve_statuses = ["idle"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.content == [TextBlock(kind="text", text="hello")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_status_reads_that_keep_dropping_still_end_the_turn_as_lost() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]
    fa.beta.sessions.retrieve_statuses = [_dropped(), _dropped(), _dropped()]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.retrieve_calls == ["sess_1"] * 3
    assert final.termination == TerminationReason.CONNECTION_LOST
    assert len(lc.terminal_failures) == 1


async def test_a_read_error_mid_stream_reconnects() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    mid = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseReadError()],
        [YieldEvent(mid), YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [pre, mid]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert lc.reconnects == ["connection_dropped"]
    assert final.content == [TextBlock(kind="text", text="hello world")]
    assert len(lc.terminal_success) == 1
    assert not lc.terminal_failures


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_a_503_on_the_status_read_after_a_clean_close_is_retried() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(pre)], [YieldEvent(done)]]
    fa.beta.sessions.events.replay_events = [pre]
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1")
    unavailable = anthropic.APIStatusError(
        "unavailable", response=httpx.Response(503, request=request), body=None
    )
    fa.beta.sessions.retrieve_statuses = [unavailable, "running"]

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_a_4xx_on_the_status_read_is_not_retried() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1")
    fa.beta.sessions.retrieve_raises = anthropic.APIStatusError(
        "not found", response=httpx.Response(404, request=request), body=None
    )

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.retrieve_calls == ["sess_1"]
    assert final.error is not None
    assert len(lc.terminal_failures) == 1


class _MuxShapedIO:
    """The legacy IO behind the errors a mux backend raises: a normalized, retryable
    `ProviderError` on the first status read and on every replay."""

    def __init__(self, fake: FakeAnthropic, *, replay_may_send: bool) -> None:
        self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")
        self.replay_may_send = replay_may_send
        self.checks = 0
        self.replays = 0

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return await self.source.open_stream(read_timeout_s=read_timeout_s)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self.source.send(events)

    async def status(self) -> str:
        self.checks += 1
        if self.checks == 1:
            raise ProviderError("overloaded", retryable=True)
        return await self.source.status()

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        self.replays += 1
        if self.replays == 1:
            raise ProviderError("overloaded", retryable=True)
        return await self.source.replay(timeout_s=timeout_s)

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return await self.source.interrupt(timeout_s=timeout_s)

    async def archive(self) -> None:
        await self.source.archive()


@pytest.mark.usefixtures("no_quiet_read_pause")
@pytest.mark.parametrize("replay_may_send", [False, True])
async def test_a_mux_backends_retryable_failure_is_retried_unless_its_replay_sends(
    monkeypatch: pytest.MonkeyPatch, replay_may_send: bool
) -> None:
    from daimon.core.turn import driver

    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(pre)]]
    fa.beta.sessions.events.replay_events = [pre, done]
    fa.beta.sessions.retrieve_statuses = ["idle"]
    io = _MuxShapedIO(fa, replay_may_send=replay_may_send)

    def factory(*args: object, **kwargs: object) -> _MuxShapedIO:
        return io

    monkeypatch.setattr(driver, "turn_io", factory)
    lc = RecordingLifecycle()
    async with asyncio.timeout(10):
        final = await driver.run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
        )

    assert io.checks == 2, "the status read is always retried"
    if replay_may_send:
        assert io.replays == 1, "a replay that can send is never repeated"
        assert len(lc.terminal_failures) == 1
    else:
        assert io.replays == 2
        assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
        assert len(lc.terminal_success) == 1


async def test_stop_while_a_failed_status_read_waits_interrupts_the_running_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.turn import driver

    monkeypatch.setattr(driver, "_QUIET_READ_RETRY_DELAYS_S", (5.0, 5.0))
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [],  # a clean close: the status read follows
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]
    fa.beta.sessions.retrieve_statuses = [_dropped(), _dropped(), _dropped()]
    cancel = asyncio.Event()

    async def press_stop() -> None:
        while not fa.beta.sessions.retrieve_calls:
            await asyncio.sleep(0.001)
        cancel.set()

    lc = RecordingLifecycle()
    async with asyncio.timeout(3):  # far less than one retry pause
        await asyncio.gather(
            press_stop(),
            run_turn_on_leg(
                anthropic=_cast(fa),
                session_id="sess_1",
                user_message="hi",
                lifecycle=lc,
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=5.0,
                now=_now,
                billing=_EXEMPT,
            ),
        )

    assert fa.beta.sessions.retrieve_calls == ["sess_1"], "no read after Stop"
    assert lc.interrupts == ["cancel_event"]
    sent_types = [
        ev["type"] for _sid, payload in fa.beta.sessions.events.sent_events for ev in payload
    ]
    assert "user.interrupt" in sent_types, "the provider is told to stop"


@pytest.mark.usefixtures("no_quiet_read_pause")
async def test_a_raw_transport_error_that_outlasts_the_retries_ends_the_turn_as_lost() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]
    request = httpx.Request("GET", "https://api.anthropic.com/v1/beta/sessions/sess_1")
    fa.beta.sessions.retrieve_raises = httpx.ReadTimeout("slow", request=request)

    lc = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert len(fa.beta.sessions.retrieve_calls) == 3
    assert final.termination == TerminationReason.CONNECTION_LOST
    assert len(lc.terminal_failures) == 1
