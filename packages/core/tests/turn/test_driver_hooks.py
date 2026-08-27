"""Driver emits new lifecycle hooks at the documented sites."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from daimon.core.turn import run_turn
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.turn_fakes import (
    BlockForever,
    DelayThenYield,
    FakeAnthropic,
    RaiseConnection,
    RaiseRateLimit,
    RaiseStreamDrop,
    RecordingLifecycle,
    YieldEvent,
)

from .conftest import make_agent_message, make_end_turn, make_status_idle

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)
_EXEMPT = BillingExempt(reason="cli-operator-run")


def _now() -> datetime:
    return _FROZEN_NOW


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


@pytest.mark.asyncio
async def test_recording_lifecycle_records_all_new_hooks() -> None:
    impl = RecordingLifecycle()
    await impl.on_sse_event(cast(RawMessageStreamEvent, object()))
    await impl.on_reconnect("connection_dropped")
    await impl.on_rate_limited(None)
    await impl.on_interrupt_sent("sigint")
    assert len(impl.sse_events) == 1
    assert impl.reconnects == ["connection_dropped"]
    assert impl.rate_limits == [None]
    assert impl.interrupts == ["sigint"]


@pytest.mark.asyncio
async def test_driver_calls_on_sse_event_for_each_upstream_event() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hello ")),
            YieldEvent(make_agent_message(event_id="sevt_2", text="world")),
            YieldEvent(make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())),
        ]
    ]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert len(lc.sse_events) == 3, (
        "driver must call on_sse_event once per upstream event (2 agent_messages + 1 status_idle)"
    )


@pytest.mark.asyncio
async def test_driver_calls_on_reconnect_on_connection_drop() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    mid = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())

    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseConnection()],
        [YieldEvent(mid), YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [pre, mid]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert lc.reconnects == ["connection_dropped"], (
        "driver must call on_reconnect with 'connection_dropped' after APIConnectionError"
    )
    assert len(lc.terminal_success) == 1, "turn must complete successfully after reconnect"


@pytest.mark.asyncio
async def test_driver_reconnects_when_sse_body_drops_mid_stream() -> None:
    """A mid-body SSE drop arrives as a raw httpx error, not APIConnectionError.

    It must still take the reconnect-and-replay path rather than escaping the
    driver as an unhandled exception.
    """
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello ")
    mid = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())

    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseStreamDrop()],
        [YieldEvent(mid), YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [pre, mid]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert lc.reconnects == ["connection_dropped"], (
        "driver must reconnect after a raw httpx.RemoteProtocolError mid-stream"
    )
    assert len(lc.terminal_success) == 1, "turn must complete successfully after reconnect"
    assert not lc.terminal_failures, "a recovered stream drop must not surface as a failure"


@pytest.mark.asyncio
async def test_driver_calls_on_rate_limited_with_until_before_sleep() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[RaiseRateLimit(retry_after_seconds=30.0)]]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert len(lc.rate_limits) == 1, "driver must call on_rate_limited once on RateLimitError"
    assert lc.rate_limits[0] == _FROZEN_NOW + timedelta(seconds=30), (
        "on_rate_limited must receive datetime computed from retry-after header via now()"
    )


@pytest.mark.asyncio
async def test_driver_calls_on_interrupt_sent_when_sigint() -> None:
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="partial")
    # First stream yields pre then blocks; second stream delivers terminal idle (interrupt ack).
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), BlockForever()],
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    lc = RecordingLifecycle()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.02)
        cancel.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=cancel,
            render_interval_s=0.001,
            interrupt_timeout_s=5.0,
            now=_now,
            billing=_EXEMPT,
        )

    assert lc.interrupts == ["cancel_event"], (
        "driver must call on_interrupt_sent with 'cancel_event' after posting user.interrupt"
    )
    sent_types = [
        ev["type"] for _sid, payload in fa.beta.sessions.events.sent_events for ev in payload
    ]
    assert "user.interrupt" in sent_types, "driver must send user.interrupt event to MA on cancel"


# --- Render loop error policy (plan 19-06) --------------------------------


class _RaisingRenderLifecycle:
    """Local test double: `on_render` raises for its first `fail_count`
    calls (or on every call, when `fail_count` is `None`), and the raised
    exception type is configurable so one class covers both the ordinary
    `Exception` policy and the `CancelledError` regression pin. Every
    other hook is recorded like `RecordingLifecycle`.
    """

    def __init__(
        self,
        *,
        fail_count: int | None,
        exc_factory: type[BaseException] = RuntimeError,
    ) -> None:
        self.fail_count = fail_count
        self.exc_factory = exc_factory
        self.render_calls = 0
        self.render_states: list[TurnState] = []
        self.terminal_success: list[TurnState] = []
        self.terminal_failures: list[tuple[TurnState, Exception]] = []

    async def on_render(self, state: TurnState) -> None:
        self.render_calls += 1
        self.render_states.append(state)
        if self.fail_count is None or self.render_calls <= self.fail_count:
            raise self.exc_factory(f"adapter render boom #{self.render_calls}")

    async def on_terminal_success(self, state: TurnState) -> None:
        self.terminal_success.append(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self.terminal_failures.append((state, err))

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        return None

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        return None

    async def on_rate_limited(self, until: datetime | None) -> None:
        return None

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        return None


def _text_of(state: TurnState) -> str:
    assert state.content, "expected at least one content block"
    block = state.content[0]
    assert isinstance(block, TextBlock), "expected a text block"
    return block.text


@pytest.mark.asyncio
async def test_render_failure_survives_and_retries_same_delta() -> None:
    # `fail_count=3` (not 1): the finalizer's own guarded final render is a
    # call in its own right, made directly by `_pump` rather than through
    # the periodic render task. With `fail_count=1` that guarded call would
    # happen to land past the failure window and "recover" by coincidence
    # even if the per-tick `except Exception` were deleted entirely --
    # failing to actually pin the catch. `fail_count=3` forces MULTIPLE
    # periodic ticks to retry the same failing delta before the terminal
    # event even arrives, which only happens if the render task survives
    # each failure and ticks again.
    fa = FakeAnthropic()
    msg1 = make_agent_message(event_id="sevt_1", text="hello ")
    msg2 = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(msg1), DelayThenYield(seconds=0.08, event=msg2), YieldEvent(done)],
    ]
    lc = _RaisingRenderLifecycle(fail_count=3)

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.005,
        now=_now,
        billing=_EXEMPT,
    )

    assert len(lc.terminal_success) == 1, (
        "the turn must still complete after several render failures"
    )
    assert lc.render_calls > 3, (
        "the render loop must survive every failure and keep ticking -- "
        "more calls than the fail_count means at least one tick succeeded "
        "after retrying"
    )
    # The anchor must not have advanced past any failed call: every retry
    # re-diffs from the SAME unchanged anchor, so consecutive failed calls
    # must carry IDENTICAL content (not just a superset) until one finally
    # succeeds.
    assert _text_of(lc.render_states[1]) == _text_of(lc.render_states[0]), (
        "a failed render must not advance the anchor -- the next tick "
        "must retry the exact same delta, not a bigger one"
    )


@pytest.mark.asyncio
async def test_render_failure_on_every_call_still_completes_the_turn() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hello")),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]
    lc = _RaisingRenderLifecycle(fail_count=None)

    state = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert state.error is None, "a render failure must never fail the turn"
    assert len(lc.terminal_success) == 1, (
        "terminal hooks must still fire even though every render attempt "
        "failed, including the guarded final render"
    )
    assert lc.render_calls >= 1, "on_render must still have been attempted"


@pytest.mark.asyncio
async def test_terminal_render_failure_does_not_block_terminal_failure_hook() -> None:
    """A render failure on the finalizer's OWN guarded render must not
    prevent `on_terminal_failure` from running, and must not turn a real
    upstream failure into something else."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[RaiseRateLimit(retry_after_seconds=1.0)]]
    lc = _RaisingRenderLifecycle(fail_count=None)

    state = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert state.error is not None, "the underlying upstream failure must still surface"
    assert len(lc.terminal_failures) == 1, (
        "on_terminal_failure must still fire even though the guarded final render failed"
    )


@pytest.mark.asyncio
async def test_cancelled_error_from_on_render_is_not_swallowed() -> None:
    """Pins `except Exception`, not `except BaseException`, in `_render_once`.

    The lifecycle raises `CancelledError` on its first call only. If the
    driver caught `BaseException` there, the render loop would treat it as
    an ordinary render failure and keep ticking for the rest of the turn
    (many more calls, one per `render_interval_s`, over the scripted
    delay). If the driver correctly lets `CancelledError` propagate, the
    render task's own loop dies on that first tick and never ticks again
    -- the only remaining call is the finalizer's own guarded final
    render (which this lifecycle lets succeed, since it only raises once).
    """
    fa = FakeAnthropic()
    msg1 = make_agent_message(event_id="sevt_1", text="hello")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(msg1), DelayThenYield(seconds=0.1, event=done)],
    ]
    lc = _RaisingRenderLifecycle(fail_count=1, exc_factory=asyncio.CancelledError)

    state = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.005,
        now=_now,
        billing=_EXEMPT,
    )

    assert state.error is None, "the turn must still complete despite the render task dying"
    assert lc.render_calls == 2, (
        "exactly one periodic tick (which raised CancelledError and killed "
        "the render task) plus the finalizer's guarded final render -- if "
        "CancelledError were caught like an ordinary Exception, the render "
        "loop would have kept ticking for the whole 0.1s delay instead"
    )
