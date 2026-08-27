"""Behavioral coverage of daimon.testing.turn_fakes' timing model.

Exercises the fake surface directly (no driver involved): the raw
`httpx.ReadTimeout` a mid-body read stall produces, a timed delay before an
event yields, per-call timeout capture, stream close tracking, and the
scripted `sessions.retrieve` status queue.
"""

from __future__ import annotations

import time

import httpx
import pytest
from daimon.testing.ma import MARouter, build_fake_anthropic, session_response
from daimon.testing.turn_fakes import (
    DelayThenYield,
    FakeEventsResource,
    FakeSessionsBeta,
    RaiseReadTimeout,
    YieldEvent,
)


async def test_raise_read_timeout_raises_httpx_read_timeout_during_iteration() -> None:
    events = FakeEventsResource(stream_scripts=[[RaiseReadTimeout()]])

    stream = await events.stream(session_id="sess_1")
    with pytest.raises(httpx.ReadTimeout):
        async for _ in stream:
            pass


async def test_delay_then_yield_delays_before_yielding_event() -> None:
    events = FakeEventsResource(
        stream_scripts=[[DelayThenYield(seconds=0.05, event={"type": "fake"})]]
    )

    stream = await events.stream(session_id="sess_1")
    started = time.monotonic()
    yielded = [event async for event in stream]
    elapsed = time.monotonic() - started

    assert yielded == [{"type": "fake"}], "DelayThenYield must yield its event"
    assert elapsed >= 0.05, "DelayThenYield must sleep before yielding"


async def test_stream_timeouts_records_the_per_call_timeout_kwarg() -> None:
    events = FakeEventsResource(
        stream_scripts=[[YieldEvent({"type": "fake"})], [YieldEvent({"type": "fake"})]]
    )

    await events.stream(session_id="sess_1", timeout=30.0)
    await events.stream(session_id="sess_1", timeout=None)

    assert events.stream_timeouts == [30.0, None], (
        "stream_timeouts must record every stream() call's timeout kwarg in order"
    )


async def test_stream_close_flips_closed() -> None:
    events = FakeEventsResource(stream_scripts=[[YieldEvent({"type": "fake"})]])

    stream = await events.stream(session_id="sess_1")

    assert stream.closed is False, "a freshly opened stream must not start closed"
    await stream.close()
    assert stream.closed is True, "close() must flip closed to True"
    assert events.streams == [stream], "streams must record every stream handed out"


async def test_retrieve_pops_statuses_in_order_and_records_calls() -> None:
    sessions = FakeSessionsBeta(retrieve_statuses=["running", "idle"])

    first = await sessions.retrieve("sess_1")
    second = await sessions.retrieve("sess_2")

    assert first.status == "running", "retrieve must pop statuses in queue order"
    assert second.status == "idle", "retrieve must pop statuses in queue order"
    assert sessions.retrieve_calls == ["sess_1", "sess_2"], (
        "retrieve_calls must record session ids in call order"
    )


async def test_retrieve_raises_assertion_error_when_statuses_queue_is_empty() -> None:
    sessions = FakeSessionsBeta(retrieve_statuses=[])

    with pytest.raises(AssertionError, match="sess_unexpected"):
        await sessions.retrieve("sess_unexpected")


async def test_retrieve_raises_propagates_the_configured_exception() -> None:
    sessions = FakeSessionsBeta(retrieve_raises=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await sessions.retrieve("sess_1")


async def test_session_response_serves_sessions_retrieve_through_a_real_sdk_client() -> None:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/[^/]+$",
        lambda request, match: session_response(session_id="sess_1", status="running"),
    )
    client = build_fake_anthropic(router.dispatch)

    session = await client.beta.sessions.retrieve("sess_1")

    assert session.status == "running", (
        "session_response must round-trip status through the real SDK client"
    )
