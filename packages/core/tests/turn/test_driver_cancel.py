"""Cancel-race coverage for the driver's setup path (19-05, Task 1).

Pre-plan, `_consume_with_reconnect` only raced `cancel.wait()` against the
consume loop's per-event fetch -- a cancel signalled while the stream-open
(`events.stream`) or `send_initial` (`events.send`) await was in flight was
simply ignored until that await resolved, which could stack up to
`MA_MAX_RETRIES=8` SDK retries at up to 600s per read. These tests pin the
new `_await_or_cancel` race covering both setup awaits: a cancel wins
immediately, no stream is leaked, and no waiter task lingers afterward.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.testing.turn_fakes import (
    BlockForever,
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


def _assert_no_leaked_turn_tasks() -> None:
    """No `turn.cancel_waiter` / `turn.stream_next` task remains pending.

    `run_turn` has already returned by the time this is called, and every
    exit path drains its waiter task in a `finally` before returning -- so
    nothing named `turn.*` should still be alive.
    """
    leaked = [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith("turn.") and not t.done()
    ]
    assert leaked == [], f"leaked turn task(s): {leaked}"


async def test_cancel_during_stream_open_interrupts_before_any_event() -> None:
    """A cancel signalled while the stream-open await is in flight raises
    `interrupted` naming `stream-open`, without ever posting the user
    message -- a turn nobody wanted must not open a session either."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    cancel = asyncio.Event()
    original_stream = fa.beta.sessions.events.stream

    async def _slow_stream(*, session_id: str, timeout: object = None):
        await asyncio.sleep(0.05)
        return await original_stream(session_id=session_id, timeout=timeout)

    fa.beta.sessions.events.stream = _slow_stream  # type: ignore[assignment]

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel.set()

    lc = RecordingLifecycle()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        final = await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=cancel,
            render_interval_s=0.001,
            now=_now,
            billing=_EXEMPT,
        )

    assert final.error is not None
    assert final.error.kind == "interrupted"
    assert "stream-open" in final.error.message, "message should identify stream-open phase"
    assert fa.beta.sessions.events.sent_events == [], (
        "no user message should be posted for a turn nobody wanted"
    )
    assert len(lc.terminal_failures) == 1
    _assert_no_leaked_turn_tasks()


async def test_cancel_during_send_initial_interrupts_and_closes_the_opened_stream() -> None:
    """A cancel signalled while `send_initial` (events.send) is in flight
    raises `interrupted` naming `send-initial`, and the stream that was
    already opened by that point is closed rather than leaked."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    cancel = asyncio.Event()
    original_send = fa.beta.sessions.events.send

    async def _slow_send(session_id: str, *, events: list[dict[str, object]]) -> None:
        await asyncio.sleep(0.05)
        await original_send(session_id, events=events)

    fa.beta.sessions.events.send = _slow_send  # type: ignore[assignment]

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel.set()

    lc = RecordingLifecycle()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        final = await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=cancel,
            render_interval_s=0.001,
            now=_now,
            billing=_EXEMPT,
        )

    assert final.error is not None
    assert final.error.kind == "interrupted"
    assert "send-initial" in final.error.message, "message should identify send-initial phase"
    assert fa.beta.sessions.events.streams[0].closed is True, (
        "a stream opened before the cancel must not be leaked"
    )
    assert len(lc.terminal_failures) == 1
    _assert_no_leaked_turn_tasks()


async def test_cancel_already_set_before_run_turn_stays_pre_stream() -> None:
    """Regression pin: a cancel already set before `run_turn` is even
    called is unaffected by the widened setup race -- it is still caught by
    the cheap `pre-stream` fast-check, with zero stream-open calls."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    cancel = asyncio.Event()
    cancel.set()

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is not None
    assert final.error.kind == "interrupted"
    assert "pre-stream" in final.error.message
    assert fa.beta.sessions.events.stream_calls == 0
    _assert_no_leaked_turn_tasks()


async def test_cancel_during_stream_open_leaves_no_waiter_task_pending() -> None:
    """No `turn.cancel_waiter` / `turn.stream_next` task lingers after a
    stream-open cancel -- the widened `finally` in `_consume_with_reconnect`
    now covers the setup awaits, not just the consume loop."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    cancel = asyncio.Event()
    original_stream = fa.beta.sessions.events.stream

    async def _slow_stream(*, session_id: str, timeout: object = None):
        await asyncio.sleep(0.05)
        return await original_stream(session_id=session_id, timeout=timeout)

    fa.beta.sessions.events.stream = _slow_stream  # type: ignore[assignment]

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel.set()

    lc = RecordingLifecycle()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=cancel,
            render_interval_s=0.001,
            now=_now,
            billing=_EXEMPT,
        )

    _assert_no_leaked_turn_tasks()


async def test_interrupt_mid_consume_still_posts_user_interrupt_regression() -> None:
    """Regression: the pre-existing consume-loop interrupt behavior is
    unchanged by widening the race to the setup awaits -- a cancel signalled
    once events are already flowing still posts `user.interrupt` and ends
    the turn as a clean terminal success on ack (mirrors
    `test_interrupt_mid_consume_posts_user_interrupt_and_ends_clean_on_ack`
    in test_driver.py, kept green by the full-suite run)."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [BlockForever()],
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    lc = RecordingLifecycle()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.02)
        cancel.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        final = await run_turn(
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

    types = [ev["type"] for _sid, payload in fa.beta.sessions.events.sent_events for ev in payload]
    assert "user.interrupt" in types
    assert len(lc.terminal_success) == 1
    assert final.error is None
    _assert_no_leaked_turn_tasks()
