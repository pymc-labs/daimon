"""Driver-level ceiling behavior: `run_turn`'s optional `deadline` kwarg.

Deliberately does NOT inject the frozen `now` these tests' siblings use
(`test_driver_reconnect.py`, `test_driver_cancel.py`). Ceiling arithmetic
(`remaining_s`) compares the injected `now()` against the deadline, so a
frozen clock would make every deadline either permanently in the past or
permanently in the future -- tests here build deadlines from
`datetime.now(UTC)` and let `run_turn` use its real-clock default. Breaches
are injected with a past or tight deadline plus a slow/blocking stream
script, never by monkeypatching a clock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.ceiling import CEILING_MESSAGE
from daimon.core.turn.posture import BillingExempt
from daimon.testing.turn_fakes import (
    BlockForever,
    DelayThenYield,
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)

from .conftest import make_end_turn, make_status_idle

_EXEMPT = BillingExempt(reason="cli-operator-run")


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


async def test_no_deadline_leaves_the_driver_unbounded() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
    )

    assert final.error is None, "a normal scripted turn with no deadline must complete cleanly"
    assert len(lc.terminal_success) == 1, "the default (no-deadline) path must be untouched"


async def test_past_deadline_returns_a_ceiling_turn_error() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[BlockForever()]]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        deadline=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert final.error is not None, "a past deadline must breach the ceiling"
    assert final.error.kind == "ceiling", "a ceiling breach must report kind='ceiling'"
    assert final.error.message == CEILING_MESSAGE, "must reuse the shared ceiling message"


async def test_ceiling_breach_delivers_on_terminal_failure_exactly_once() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[BlockForever()]]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        deadline=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert len(lc.terminal_failures) == 1, "on_terminal_failure must be delivered exactly once"
    _, err = lc.terminal_failures[0]
    assert getattr(err, "kind", None) == "ceiling", "the delivered error must be the ceiling kind"
    assert lc.terminal_success == [], "a breach must never also deliver on_terminal_success"


async def test_ceiling_breach_closes_the_opened_stream() -> None:
    fa = FakeAnthropic()
    terminal = make_status_idle(event_id="sevt_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[DelayThenYield(seconds=5.0, event=terminal)]]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        deadline=datetime.now(UTC) + timedelta(seconds=0.2),
    )

    assert fa.beta.sessions.events.streams[0].closed is True, (
        "a ceiling breach must close the SSE stream it abandons, not leak the connection"
    )


async def test_a_future_deadline_does_not_disturb_a_normal_turn() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )

    assert final.error is None, "a deadline far in the future must not disturb a normal turn"
    assert len(lc.terminal_success) == 1, "a normal turn under a generous deadline must succeed"


async def test_ceiling_breach_during_stream_open_leaks_no_task_and_no_stream() -> None:
    """A ceiling breach landing while the stream-open await is still in flight
    must not orphan `_await_or_cancel`'s work task.

    The breach cancels `run_turn` from OUTSIDE (`asyncio.wait_for`), so neither
    branch of the helper's cancel-vs-work race runs. Without an explicit
    release the work task outlives the turn, and when it later completes it
    opens an SSE stream nobody holds a reference to -- the caller's
    `opened_stream` is only assigned once `_await_or_cancel` RETURNS, which on
    this path it never does.
    """
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    original_stream = fa.beta.sessions.events.stream

    async def _slow_stream(*, session_id: str, timeout: object = None):
        await asyncio.sleep(0.20)  # still in flight when the ceiling fires
        return await original_stream(session_id=session_id, timeout=timeout)

    fa.beta.sessions.events.stream = _slow_stream  # type: ignore[assignment]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        deadline=datetime.now(UTC) + timedelta(milliseconds=50),
    )

    assert final.error is not None
    assert final.error.kind == "ceiling"
    # Scoped to the setup tasks `_await_or_cancel` owns. `turn.render_loop` is
    # deliberately excluded: the outer `finally` cancels it without draining, so
    # it is routinely cancelled-but-not-yet-reaped for one more loop turn --
    # a different (and pre-existing) question from orphaning a setup await.
    orphaned = [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name() in {"turn.stream_open", "turn.send_initial"} and not t.done()
    ]
    assert orphaned == [], f"the breach must not orphan a setup task: {orphaned}"

    # Let anything the orphan would have done happen, then prove it didn't.
    await asyncio.sleep(0.30)
    assert fa.beta.sessions.events.streams == [], (
        "a cancelled stream-open must never go on to open a stream nobody closes"
    )
