"""Driver emits new lifecycle hooks at the documented sites."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from daimon.core.turn import run_turn

from .conftest import make_agent_message, make_end_turn, make_status_idle
from .fakes import (
    BlockForever,
    FakeAnthropic,
    RaiseConnection,
    RaiseRateLimit,
    RecordingLifecycle,
    YieldEvent,
)

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)


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
    )

    assert lc.reconnects == ["connection_dropped"], (
        "driver must call on_reconnect with 'connection_dropped' after APIConnectionError"
    )
    assert len(lc.terminal_success) == 1, "turn must complete successfully after reconnect"


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
        )

    assert lc.interrupts == ["cancel_event"], (
        "driver must call on_interrupt_sent with 'cancel_event' after posting user.interrupt"
    )
    sent_types = [
        ev["type"] for _sid, payload in fa.beta.sessions.events.sent_events for ev in payload
    ]
    assert "user.interrupt" in sent_types, "driver must send user.interrupt event to MA on cancel"
