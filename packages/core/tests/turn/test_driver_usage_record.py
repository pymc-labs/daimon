"""Driver invokes usage_record on span.model_request_end events. BILL-02."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.turn import run_turn

from .conftest import make_agent_message, make_end_turn, make_status_idle
from .fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

_T = datetime(2026, 1, 1, tzinfo=UTC)


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


def _make_model_request_end(
    *, event_id: str = "sevt_mre_1"
) -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        type="span.model_request_end",
        model_request_start_id="mrs_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            input_tokens=10,
            output_tokens=5,
        ),
        processed_at=_T,
        is_error=False,
    )


async def test_driver_invokes_usage_record_on_model_request_end_event() -> None:
    mre = _make_model_request_end(event_id="sevt_mre_1")
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hi")),
            YieldEvent(mre),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    calls: list[dict[str, object]] = []

    async def fake_usage_record(*, event: object, session_id: str) -> None:
        calls.append({"event": event, "session_id": session_id})

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_123",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        usage_record=fake_usage_record,
    )

    assert len(calls) == 1, (
        "usage_record invoked exactly once for the single model_request_end event"
    )
    assert calls[0]["event"] is mre, "usage_record received the model_request_end event"
    assert calls[0]["session_id"] == "sess_123", "usage_record received the session_id kwarg"


async def test_driver_does_not_invoke_usage_record_on_other_events() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hi")),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    calls: list[object] = []

    async def fake_usage_record(*, event: object, session_id: str) -> None:
        calls.append(event)

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        usage_record=fake_usage_record,
    )

    assert calls == [], "usage_record must not be invoked when no model_request_end events stream"


async def test_driver_with_usage_record_none_skips_invocation() -> None:
    """Default usage_record=None: presence of model_request_end events is a no-op."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(_make_model_request_end()),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
    )

    assert final.stop_reason is not None, "turn reached terminal idle without usage_record"


async def test_driver_propagates_usage_record_exception() -> None:
    """D-25 fail-closed: usage_record raise propagates uncaught."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(_make_model_request_end()),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    async def boom_usage_record(*, event: object, session_id: str) -> None:
        raise RuntimeError("metering down")

    with pytest.raises(RuntimeError, match="metering down"):
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            usage_record=boom_usage_record,
        )
