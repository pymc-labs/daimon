"""A session left waiting on confirmations no turn will answer.

Staging, 2026-10-09: a worker restart cancelled turns whose approval cards were
up before their denials went out. MA kept waiting on those calls and refused
every later user.message with a 400, so the thread was stuck for good. The
driver now interrupts the session once and starts the turn over.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import anthropic
import httpx
from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

from .conftest import make_agent_message, make_end_turn, make_requires_action, make_status_idle

_EXEMPT = BillingExempt(reason="cli-operator-run")
_STUCK = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Invalid user.message event at events[0]: waiting on responses to events "
    "[sevt_1, sevt_2]; only user.tool_confirmation, user.custom_tool_result, "
    "user.tool_result, or user.interrupt may be sent'}}"
)


def _now() -> datetime:
    return datetime(2026, 10, 9, 17, 0, tzinfo=UTC)


def _refusal() -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions/sess_1/events")
    return anthropic.BadRequestError(
        _STUCK, response=httpx.Response(400, request=request), body=None
    )


def _refuse_messages(fa: FakeAnthropic, *, times: int, interrupt_lag_s: float = 0.0) -> None:
    """Make the first `times` user.message sends fail as a stuck session does.

    The session starts idle on a `requires_action` pause, as MA leaves it.
    MA takes an interrupt `interrupt_lag_s` later: its pause becomes an
    end_turn, and until then every user.message is refused as well.
    """
    resource = fa.beta.sessions.events
    real_send = resource.send
    left = [times]
    resource.replay_events.append(
        make_status_idle(
            event_id="sevt_pause",
            stop_reason=make_requires_action(event_ids=["sevt_1", "sevt_2"]),
        )
    )
    interrupted = asyncio.Event()

    def _take_interrupt() -> None:
        resource.replay_events.append(make_status_idle(event_id="sevt_interrupted"))
        interrupted.set()

    async def send(session_id: str, *, events: list[dict[str, Any]]) -> None:
        stuck = left[0] > 0 or not interrupted.is_set()
        if events and events[0]["type"] == "user.message" and stuck:
            left[0] = max(left[0] - 1, 0)
            raise _refusal()
        await real_send(session_id, events=events)
        if events and events[0]["type"] == "user.interrupt":
            asyncio.get_running_loop().call_later(interrupt_lag_s, _take_interrupt)

    resource.send = send  # type: ignore[method-assign]


async def test_a_stuck_session_is_interrupted_and_the_turn_runs() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [],  # the first attempt's stream: the send is refused before any event
        [
            YieldEvent(make_agent_message(event_id="sevt_a", text="hello again")),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ],
    ]
    fa.beta.sessions.retrieve_statuses = ["idle"] * 20
    _refuse_messages(fa, times=1)

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="are you there?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is None
    assert final.content == [TextBlock(kind="text", text="hello again")]
    sent = [batch[0]["type"] for _sid, batch in fa.beta.sessions.events.sent_events]
    assert sent == ["user.interrupt", "user.message"], "interrupt first, then the message once"


async def test_a_session_still_stuck_after_the_interrupt_says_start_a_new_thread() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[], []]
    fa.beta.sessions.retrieve_statuses = ["idle"] * 20
    _refuse_messages(fa, times=2)

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="are you there?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is not None and final.error.kind == "upstream"
    sent = [batch[0]["type"] for _sid, batch in fa.beta.sessions.events.sent_events]
    assert sent == ["user.interrupt"], "one interrupt only, never a loop"
    assert final.termination is not None
    notice = render_termination_notice(final.termination, state=final)
    assert notice is not None
    assert notice.next_step == "Start a new thread to carry on."
    assert "stuck" in notice.headline.lower()


async def test_a_failed_interrupt_still_ends_the_turn_through_the_normal_failure_path() -> None:
    """Review of #542: a recovery error must not escape without terminal callbacks."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[], []]
    _refuse_messages(fa, times=1)
    events = fa.beta.sessions.events
    refusing_send = events.send

    async def send(session_id: str, *, events: list[dict[str, Any]]) -> None:
        if events and events[0]["type"] == "user.interrupt":
            request = httpx.Request("POST", "https://api.anthropic.com/x")
            raise anthropic.InternalServerError(
                "boom", response=httpx.Response(500, request=request), body=None
            )
        await refusing_send(session_id, events=events)

    events.send = send  # type: ignore[method-assign]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="are you there?",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is not None and final.error.kind == "upstream"
    assert len(lc.terminal_failures) == 1, "the normal failure callbacks ran"
    assert final.termination is not None
    notice = render_termination_notice(final.termination, state=final)
    assert notice is not None and notice.next_step == "Start a new thread to carry on."


async def test_stop_during_recovery_ends_the_turn_as_stopped() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[], []]
    fa.beta.sessions.retrieve_statuses = ["running"] * 1000
    _refuse_messages(fa, times=1)
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, cancel.set)

    final = await asyncio.wait_for(
        run_turn(
            anthropic=cast(AsyncAnthropic, fa),
            session_id="sess_1",
            user_message="are you there?",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            render_interval_s=0.001,
            now=_now,
            billing=_EXEMPT,
        ),
        timeout=5,
    )

    assert final.error is not None and final.error.kind == "interrupted"


def test_the_stuck_notice_is_typed_not_any_matching_text() -> None:
    from daimon.core.errors import TurnError
    from daimon.core.turn.state import TurnState
    from daimon.core.turn.termination import TerminationReason

    loose = TurnState(error=TurnError(kind="interrupted", message=_STUCK))
    notice = render_termination_notice(TerminationReason.INTERRUPTED, state=loose)
    assert notice is not None and "stuck" not in notice.headline.lower()


async def test_a_failed_recovery_never_opens_another_stream() -> None:
    """Re-review of #542: the failure is finalized before any stream opens, so a
    stream-open error cannot send the turn down the replay path instead."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]  # only the first attempt's stream
    _refuse_messages(fa, times=1)
    events = fa.beta.sessions.events
    refusing_send = events.send

    async def send(session_id: str, *, events: list[dict[str, Any]]) -> None:
        if events and events[0]["type"] == "user.interrupt":
            request = httpx.Request("POST", "https://api.anthropic.com/x")
            raise anthropic.InternalServerError(
                "boom", response=httpx.Response(500, request=request), body=None
            )
        await refusing_send(session_id, events=events)

    events.send = send  # type: ignore[method-assign]

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="are you there?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert fa.beta.sessions.events.stream_calls == 1, "no second stream after a failed recovery"
    assert final.error is not None and final.error.kind == "upstream"


async def test_the_retry_waits_until_ma_has_taken_the_interrupt() -> None:
    """Staging, 2026-10-09: the stuck session already read `idle`, so the retry
    went out before MA took the interrupt and was refused again."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [],
        [
            YieldEvent(make_agent_message(event_id="sevt_a", text="hello again")),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ],
    ]
    fa.beta.sessions.retrieve_statuses = ["idle"] * 20
    _refuse_messages(fa, times=1, interrupt_lag_s=0.6)

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="are you there?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.error is None
    assert final.content == [TextBlock(kind="text", text="hello again")]
    sent = [batch[0]["type"] for _sid, batch in fa.beta.sessions.events.sent_events]
    assert sent == ["user.interrupt", "user.message"]
