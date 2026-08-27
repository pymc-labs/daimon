"""Driver invokes the bound recorder on span.model_request_end events under
a `Billed` posture, and logs exactly once under `BillingExempt`. BILL-02.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
import structlog.testing
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.turn import run_turn
from daimon.core.turn.posture import Billed, BillingExempt
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

from .conftest import make_agent_message, make_end_turn, make_status_idle

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


async def test_driver_invokes_billed_record_on_model_request_end_event() -> None:
    mre = _make_model_request_end(event_id="sevt_mre_1")
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hi")),
            YieldEvent(mre),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []

    async def fake_record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        calls.append(event)

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_123",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=Billed(record=fake_record),
    )

    assert len(calls) == 1, "record invoked exactly once for the single model_request_end event"
    assert calls[0] is mre, "record received the model_request_end event"


async def test_driver_does_not_invoke_billed_record_on_other_events() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hi")),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []

    async def fake_record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        calls.append(event)

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=Billed(record=fake_record),
    )

    assert calls == [], "record must not be invoked when no model_request_end events stream"


async def test_driver_propagates_billed_record_exception() -> None:
    """Fail-closed: record raise propagates uncaught."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(_make_model_request_end()),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    async def boom_record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        raise RuntimeError("metering down")

    with pytest.raises(RuntimeError, match="metering down"):
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=Billed(record=boom_record),
        )


async def test_driver_billing_exempt_invokes_no_recorder() -> None:
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
        billing=BillingExempt(reason="cli-operator-run"),
    )

    assert final.stop_reason is not None, "turn reached terminal idle under BillingExempt"


async def test_driver_billing_exempt_logs_exactly_once_per_turn_with_multiple_events() -> None:
    """Two span.model_request_end events in one turn must still produce
    exactly one `turn.billing_exempt` log entry (RESEARCH Pitfall 5)."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(_make_model_request_end(event_id="sevt_mre_1")),
            YieldEvent(_make_model_request_end(event_id="sevt_mre_2")),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]

    with structlog.testing.capture_logs() as logs:
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="cli-operator-run"),
        )

    exempt_logs = [e for e in logs if e["event"] == "turn.billing_exempt"]
    assert len(exempt_logs) == 1, (
        "exactly one turn.billing_exempt log entry per exempt turn, "
        "even with multiple model_request_end events"
    )
    assert exempt_logs[0]["session_id"] == "sess_1"
    assert exempt_logs[0]["reason"] == "cli-operator-run"


async def test_run_turn_without_billing_raises_type_error() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]

    with pytest.raises(TypeError):
        await run_turn(  # type: ignore[call-arg]
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
        )
