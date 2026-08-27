"""Scenario matrix for the eventless-cycle reconnect loop (Class A fix).

Task 1 covers only the injectable read-timeout kwarg threaded to
`events.stream(...)`. Task 2 extends this file with the status-checked
eventless-cycle decision procedure (clean close / read timeout / status
grouping / the untouched connection-error budget).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import anthropic
import httpx
from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock
from daimon.testing.turn_fakes import (
    FakeAnthropic,
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


async def test_run_turn_passes_default_stream_read_timeout_to_events_stream() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]

    await run_turn(
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

    await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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
    final = await run_turn(
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

    final = await run_turn(
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

    await run_turn(
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
    final = await run_turn(
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
