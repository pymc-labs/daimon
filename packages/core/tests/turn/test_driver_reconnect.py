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

import httpx
from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.testing.turn_fakes import (
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)

from .conftest import make_end_turn, make_status_idle

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
