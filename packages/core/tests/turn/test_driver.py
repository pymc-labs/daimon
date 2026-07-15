from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.errors import TurnError
from daimon.core.turn import run_turn
from daimon.core.turn.state import TextBlock

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_requires_action,
    make_status_idle,
    make_status_terminated,
)
from .fakes import (
    BlockForever,
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FROZEN_NOW


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


async def test_run_turn_folds_full_stream_and_returns_terminal_state() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hello ")),
            YieldEvent(make_agent_message(event_id="sevt_2", text="world")),
            YieldEvent(make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())),
        ]
    ]
    lc = RecordingLifecycle()
    cancel = asyncio.Event()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
    )

    assert final.content == [TextBlock(kind="text", text="hello world")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1
    assert lc.terminal_failures == []


async def test_run_turn_finalizes_session_status_terminated_as_terminal_failure() -> None:
    """`session.status_terminated` has no SDK stop_reason -- pre-fix, the
    consume loop's `terminal_stop_reason(event)` check never matches it, so
    the loop keeps waiting for another event on a stream that emits nothing
    further (bounded here by BlockForever + a wait_for so the RED failure is
    a timeout, not a hang). Post-fix, the driver exits the consume loop
    immediately on the terminated event and finalizes via on_terminal_failure
    with kind='upstream'."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="hello")),
            YieldEvent(make_status_terminated(event_id="sevt_2")),
            BlockForever(),
        ]
    ]
    lc = RecordingLifecycle()

    final = await asyncio.wait_for(
        run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            now=_now,
        ),
        timeout=2.0,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert len(lc.terminal_failures) == 1
    assert lc.terminal_success == []


async def test_run_turn_finalizes_requires_action_idle_as_actionable_failure() -> None:
    """`requires_action` idle means the agent is paused on a tool needing
    approval. The interactive driver has no approval/resume UX -- pre-fix
    this finalizes as a blank on_terminal_success; post-fix it must finalize
    via on_terminal_failure with an actionable TurnError."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1"]),
                )
            ),
        ]
    ]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert "tool approval" in final.error.message
    assert len(lc.terminal_failures) == 1
    assert lc.terminal_success == []


async def test_run_turn_posts_user_message_after_opening_stream() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
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

    assert fa.beta.sessions.events.stream_calls == 1
    assert len(fa.beta.sessions.events.sent_events) == 1
    sid, payload = fa.beta.sessions.events.sent_events[0]
    assert sid == "sess_1"
    assert payload == [
        {
            "type": "user.message",
            "content": [{"type": "text", "text": "hi"}],
        }
    ], "run_turn must send user.message with a text-block array"


async def test_run_turn_prepends_image_blocks_before_text_block() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="sevt_1", stop_reason=make_end_turn()))]
    ]
    lc = RecordingLifecycle()
    image_block: BetaManagedAgentsImageBlockParam = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
    }

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="what's in this image?",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        image_blocks=[image_block],
    )

    assert len(fa.beta.sessions.events.sent_events) == 1
    _, payload = fa.beta.sessions.events.sent_events[0]
    assert payload == [
        {
            "type": "user.message",
            "content": [
                image_block,
                {"type": "text", "text": "what's in this image?"},
            ],
        }
    ], "image blocks must precede the text block in the user.message content array"


async def test_run_turn_always_fires_final_render_before_terminal_success() -> None:
    """_render_once(state) must fire synchronously after the terminal event
    folds, before on_terminal_success."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="sevt_1", text="final content")),
            YieldEvent(make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())),
        ]
    ]
    lc = RecordingLifecycle()

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        # Large tick interval -> no tick will fire during the turn. Only the
        # synchronous final-render guarantee can surface "final content".
        render_interval_s=60.0,
        now=_now,
    )

    assert lc.renders, "on_render was never called; final-render guarantee violated"
    last_render = lc.renders[-1]
    assert last_render.content == [TextBlock(kind="text", text="final content")]
    assert last_render.stop_reason is not None
    assert len(lc.terminal_success) == 1


async def test_reconnect_refolds_from_empty_and_continues_the_turn() -> None:
    """On APIConnectionError the driver replays full history, re-folds from
    empty, and reattaches. State is bit-identical to a live-only run."""
    fa = FakeAnthropic()

    pre = make_agent_message(event_id="sevt_1", text="hello ")
    mid = make_agent_message(event_id="sevt_2", text="world")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())

    # First stream yields `pre`, then raises APIConnectionError.
    # replay_events returns [pre, mid] (server has it all).
    # Second stream yields `mid` (redelivered, dedup) and `done`.
    from .fakes import RaiseConnection

    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseConnection()],
        [YieldEvent(mid), YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [pre, mid]

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )
    assert final.content == [TextBlock(kind="text", text="hello world")]
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


async def test_reconnect_does_not_re_emit_pre_reconnect_content() -> None:
    """`prev` is NOT reset on reconnect. The diff machinery masks the
    re-delivered events."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="before ")
    mid = make_agent_message(event_id="sevt_2", text="after")
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    from .fakes import RaiseConnection

    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseConnection()],
        [YieldEvent(pre), YieldEvent(mid), YieldEvent(done)],  # MA redelivers pre
    ]
    fa.beta.sessions.events.replay_events = [pre]

    lc = RecordingLifecycle()
    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        # Fast ticks to force renders around reconnect.
        render_interval_s=0.001,
        now=_now,
    )

    # No render state should show text shrinking or non-monotonic content.
    prev_text_len = 0
    for rs in lc.renders:
        if rs.content and isinstance(rs.content[0], TextBlock):
            assert len(rs.content[0].text) >= prev_text_len, (
                "content went backwards; diff anchor was reset on reconnect"
            )
            prev_text_len = len(rs.content[0].text)


async def test_double_connection_error_surfaces_connection_lost() -> None:
    """Second APIConnectionError (tenacity retry exhausted) →
    TurnError(kind="connection_lost")."""
    fa = FakeAnthropic()
    from .fakes import RaiseConnection

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
    )

    assert final.error is not None
    assert final.error.kind == "connection_lost"
    assert len(lc.terminal_failures) == 1
    assert isinstance(lc.terminal_failures[0][1], TurnError)


async def test_non_retryable_status_error_surfaces_upstream() -> None:
    """APIStatusError (non-429) does not retry; converts to
    TurnError(kind="upstream")."""
    fa = FakeAnthropic()
    from .fakes import RaiseStatus

    fa.beta.sessions.events.stream_scripts = [[RaiseStatus(status_code=500)]]
    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert fa.beta.sessions.events.stream_calls == 1, "status error must not retry"


async def test_upstream_error_clears_stop_reason() -> None:
    """_finalize_upstream must clear stop_reason so callers don't loop on stale state."""
    from .fakes import RaiseStatus

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[RaiseStatus(status_code=400)]]

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert final.stop_reason is None, (
        "stop_reason must be cleared on error to prevent infinite loops"
    )


async def test_rate_limit_error_populates_rate_limit_until_from_retry_after() -> None:
    from datetime import timedelta

    from .fakes import RaiseRateLimit

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[RaiseRateLimit(retry_after_seconds=30.0)]]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert final.rate_limit_until == _FROZEN_NOW + timedelta(seconds=30)


async def test_inband_rate_limited_session_error_wraps_but_rate_limit_until_stays_none() -> None:
    """SDK's RateLimitedError event variant carries no retry-after. The
    reducer wraps it as TurnError(upstream); rate_limit_until remains None
    (populated only from the exception path)."""
    from .conftest import make_session_error

    fa = FakeAnthropic()
    err_event = make_session_error(event_id="e_1", message="too many")
    idle = make_status_idle(event_id="s_1", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(err_event), YieldEvent(idle)]]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "upstream"
    assert final.rate_limit_until is None


async def test_interrupt_during_replay_raises_interrupted_without_posting_user_interrupt() -> None:
    from .fakes import RaiseConnection

    fa = FakeAnthropic()
    # Stream 1 raises APIConnectionError; tenacity will retry.
    fa.beta.sessions.events.stream_scripts = [
        [RaiseConnection()],
        # If we got here, interrupt didn't fire. Leave a dummy to help diagnose.
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()

    # Replay is called on retry. Intercept it to set cancel before the
    # replay iterator is consumed. Note: events.list is a sync function
    # that returns an async iterator, so the replacement must also be a
    # plain `def` (not `async def`).
    original_list = fa.beta.sessions.events.list

    def _list_triggering_cancel(*, session_id: str):
        cancel.set()
        return original_list(session_id=session_id)

    fa.beta.sessions.events.list = _list_triggering_cancel  # type: ignore[assignment]

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "interrupted"
    # No `user.interrupt` may have been posted during recovery.
    for _sid, payload in fa.beta.sessions.events.sent_events:
        for ev in payload:
            assert ev.get("type") != "user.interrupt"


async def test_interrupt_during_reattach_raises_interrupted_without_user_interrupt() -> None:
    """Cancel observed between replay completion and the post-reattach
    cancel-check raises `_InterruptedDuringRecovery(reattach)` and routes
    to on_terminal_failure without posting `user.interrupt`."""
    from .fakes import RaiseConnection

    fa = FakeAnthropic()
    # Attempt 1 raises APIConnectionError to enter retry. Attempt 2 opens a
    # fresh stream after replay -- intercept that open to set cancel before
    # the post-reattach cancel-check fires.
    fa.beta.sessions.events.stream_scripts = [
        [RaiseConnection()],
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    original_stream = fa.beta.sessions.events.stream

    async def _stream_triggering_cancel(*, session_id: str):
        result = await original_stream(session_id=session_id)
        # Post-replay, post-reattach-open. The driver's next cancel-check
        # should observe this and raise `_InterruptedDuringRecovery(reattach)`.
        if fa.beta.sessions.events.stream_calls == 2:
            cancel.set()
        return result

    fa.beta.sessions.events.stream = _stream_triggering_cancel  # type: ignore[assignment]

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "interrupted", "reattach-phase interrupt must surface as interrupted"
    assert "reattach" in final.error.message, "message should identify reattach phase"
    for _sid, payload in fa.beta.sessions.events.sent_events:
        for ev in payload:
            assert ev.get("type") != "user.interrupt", (
                "reattach-phase interrupt must not post user.interrupt"
            )


async def test_interrupt_before_stream_open_raises_interrupted_with_pre_stream_phase() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    cancel = asyncio.Event()
    cancel.set()  # interrupt before any work

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
    )

    assert final.error is not None
    assert final.error.kind == "interrupted"
    assert fa.beta.sessions.events.stream_calls == 0


async def test_interrupt_mid_consume_posts_user_interrupt_and_ends_clean_on_ack() -> None:
    from .fakes import BlockForever

    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="partial")
    # First stream: yield pre, then block (never terminate) so we can cancel.
    # Second stream: ack-waiter stream -- yield terminal idle.
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
        final = await run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="hi",
            lifecycle=lc,
            cancel=cancel,
            render_interval_s=0.001,
            interrupt_timeout_s=5.0,
            now=_now,
        )

    # user.interrupt was posted.
    types = [ev["type"] for _sid, payload in fa.beta.sessions.events.sent_events for ev in payload]
    assert "user.interrupt" in types
    # Clean ack -> on_terminal_success.
    assert len(lc.terminal_success) == 1
    assert final.content == [TextBlock(kind="text", text="partial")]


async def test_interrupt_mid_consume_timeout_surfaces_interrupt_timeout() -> None:
    from .fakes import BlockForever

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [BlockForever()],
        [BlockForever()],  # ack-waiter never sees terminal idle
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
            interrupt_timeout_s=0.05,
            now=_now,
        )

    assert final.error is not None
    assert final.error.kind == "interrupt_timeout"
    assert len(lc.terminal_failures) == 1


async def test_structlog_emits_turn_started_completed_on_happy_path() -> None:
    import structlog.testing

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="s", stop_reason=make_end_turn()))]
    ]
    lc = RecordingLifecycle()

    with structlog.testing.capture_logs() as logs:
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_42",
            user_message="hi",
            lifecycle=lc,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            now=_now,
        )

    names = [e["event"] for e in logs]
    assert "turn.started" in names
    assert "turn.completed" in names
    started = next(e for e in logs if e["event"] == "turn.started")
    assert started["session_id"] == "sess_42"
    assert started["entry"] == "run"


async def test_structlog_emits_reconnect_events_on_retry() -> None:
    import structlog.testing

    from .fakes import RaiseConnection

    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="a")
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), RaiseConnection()],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [pre]
    lc = RecordingLifecycle()

    with structlog.testing.capture_logs() as logs:
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_42",
            user_message="hi",
            lifecycle=lc,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            now=_now,
        )

    names = [e["event"] for e in logs]
    assert "turn.reconnect.started" in names
    assert "turn.reconnect.completed" in names
    completed = next(e for e in logs if e["event"] == "turn.reconnect.completed")
    assert completed["replayed"] == 1


async def test_structlog_turn_rate_limited_carries_retry_after_s_and_until() -> None:
    """`turn.rate_limited` kwargs include `session_id`, `retry_after_s`,
    `until`. `retry_after_s` is the raw header value to avoid clock round-trip."""
    import structlog.testing

    from .fakes import RaiseRateLimit

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[RaiseRateLimit(retry_after_seconds=30.0)]]
    lc = RecordingLifecycle()

    with structlog.testing.capture_logs() as logs:
        await run_turn(
            anthropic=_cast(fa),
            session_id="sess_42",
            user_message="hi",
            lifecycle=lc,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            now=_now,
        )

    rate_limited = next(e for e in logs if e["event"] == "turn.rate_limited")
    assert rate_limited["session_id"] == "sess_42", "session_id must be logged"
    assert rate_limited["retry_after_s"] == 30.0, (
        "retry_after_s must be the raw header value, not derived from until-now"
    )
    assert "until" in rate_limited, "until must be logged as ISO-format absolute"


async def test_reconnect_on_reused_session_two_turn_log_renders_only_current_turn() -> None:
    """Reconnect on a reused (multi-turn) session must not leak prior-turn text.

    The replay event log contains both turn-1 events (ending with status_idle)
    and the in-progress turn-2 events. The driver must scope the fold to events
    AFTER the last session.status_idle boundary so the render state only shows
    turn-2 content.
    """
    from .fakes import RaiseConnection

    fa = FakeAnthropic()

    # Turn 1 events (prior turn — the 'reused session' history)
    t1_msg = make_agent_message(event_id="sevt_t1", text="FIRST_TURN_REPLY")
    t1_idle = make_status_idle(event_id="idle_t1", stop_reason=make_end_turn())

    # Turn 2 events (current turn — in progress when reconnect happens)
    t2_msg_partial = make_agent_message(event_id="sevt_t2a", text="SECOND_")
    t2_msg_rest = make_agent_message(event_id="sevt_t2b", text="TURN_REPLY")
    t2_idle = make_status_idle(event_id="idle_t2", stop_reason=make_end_turn())

    # First stream: yields t2_msg_partial, then drops
    # replay_events returns full session log: t1_msg + t1_idle + t2_msg_partial
    # Second stream: redelivers t2_msg_partial (dedup) + t2_msg_rest + t2_idle
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(t2_msg_partial), RaiseConnection()],
        [YieldEvent(t2_msg_partial), YieldEvent(t2_msg_rest), YieldEvent(t2_idle)],
    ]
    fa.beta.sessions.events.replay_events = [t1_msg, t1_idle, t2_msg_partial]

    lc = RecordingLifecycle()
    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_reused",
        user_message="hi turn 2",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
    )

    from daimon.core.turn.state import TextBlock

    final_text = "".join(
        b.text
        for b in final.content
        if isinstance(b, TextBlock)  # type: ignore[union-attr]
    )
    assert "SECOND_TURN_REPLY" in final_text, "current turn content must appear in final state"
    assert "FIRST_TURN_REPLY" not in final_text, (
        "reused-session reconnect must not leak prior turn text into current turn render"
    )


def test_driver_source_contains_no_except_cancelled_error() -> None:
    """Driver contains no `except asyncio.CancelledError`. The only acceptable
    pattern is `_suppress_task_exc`'s `except BaseException:` when draining
    the cancelled render task.
    """
    import pathlib

    path = pathlib.Path("packages/core/daimon/core/turn/driver.py")
    src = path.read_text()
    assert "except asyncio.CancelledError" not in src
    assert "except CancelledError" not in src
