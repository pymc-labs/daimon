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

from .conftest import make_agent_message, make_end_turn, make_status_idle

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


def _refuse_messages(fa: FakeAnthropic, *, times: int) -> None:
    """Make the first `times` user.message sends fail as a stuck session does."""
    events = fa.beta.sessions.events
    real_send = events.send
    left = [times]

    async def send(session_id: str, *, events: list[dict[str, Any]]) -> None:
        if events and events[0]["type"] == "user.message" and left[0] > 0:
            left[0] -= 1
            raise _refusal()
        await real_send(session_id, events=events)

    events.send = send  # type: ignore[method-assign]


async def test_a_stuck_session_is_interrupted_and_the_turn_runs() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [],  # the first attempt's stream: the send is refused before any event
        [
            YieldEvent(make_agent_message(event_id="sevt_a", text="hello again")),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ],
    ]
    fa.beta.sessions.retrieve_statuses = ["idle"]
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
    fa.beta.sessions.retrieve_statuses = ["idle"]
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
