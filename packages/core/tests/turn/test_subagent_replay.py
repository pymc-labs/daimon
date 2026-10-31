"""Recorded coordinator streams through the real driver, on whichever leg CI selects.

`fixtures/ma_subagents/` holds primary streams recorded on staging: a Sonnet lead
delegating to two helpers. The primary thread idles `end_turn` before the helpers
finish; only `session.status_idle` means every thread is done, and the driver must
end there (both the legacy and the mux legs run this file in CI).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic._models import construct_type
from anthropic.types.beta.sessions.beta_managed_agents_stream_session_events import (
    BetaManagedAgentsStreamSessionEvents,
)
from daimon.core.turn.card_state import CardState, update_activity
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import Task, TextBlock, TurnState
from daimon.core.turn.status_lines import format_helper_line
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import (
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
    run_turn_on_leg,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ma_subagents"
SESSION = "sesn_01QZ6tcns4Sajq25PiciqxTg"


def _raw(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def _events(name: str) -> list[Any]:
    return [construct_type(type_=BetaManagedAgentsStreamSessionEvents, value=r) for r in _raw(name)]


async def _replay(name: str) -> tuple[TurnState, RecordingLifecycle]:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(e) for e in _events(name)]]
    lifecycle = RecordingLifecycle()
    final = await run_turn_on_leg(
        anthropic=cast(AsyncAnthropic, fa),
        session_id=SESSION,
        user_message="do two things in parallel",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=BillingExempt(reason="cli-operator-run"),
    )
    return final, lifecycle


async def test_a_fan_out_ends_when_every_helper_is_done_not_when_the_lead_first_idles() -> None:
    raw = _raw("fanout.jsonl")
    early = next(
        i
        for i, e in enumerate(raw)
        if e["type"] == "session.thread_status_idle"
        and e["stop_reason"]["type"] == "end_turn"
        and e.get("agent_name") == "subagents-spike-lead"
    )
    assert early < len(raw) - 5, "the lead idled while its helpers still worked"

    final, lifecycle = await _replay("fanout.jsonl")

    assert (len(lifecycle.terminal_success), lifecycle.terminal_failures) == (1, [])
    texts = [b.text for b in final.content if isinstance(b, TextBlock)]
    assert texts[-1].startswith("Both helpers finished"), "the lead's merged answer is last"
    assert [h.status for h in final.tasks] == ["done", "done"]
    assert format_helper_line(final.tasks) == "☑️ 2 helpers: 2 done"


async def test_a_fan_out_stopped_by_its_spending_limit_is_not_a_reply() -> None:
    final, lifecycle = await _replay("budget_reached.jsonl")

    assert lifecycle.terminal_success == []
    assert len(lifecycle.terminal_failures) == 1
    assert final.termination is TerminationReason.BUDGET_REACHED
    assert TerminationReason.BUDGET_REACHED.is_failure
    assert [t.status for t in final.tasks] == ["stopped", "stopped"], "paused, not done"


def test_the_card_shows_the_helpers_while_they_work() -> None:
    state = TurnState()
    for event in _events("fanout.jsonl"):
        state = apply(state, event)
        if event.type == "agent.thread_message_sent" and len(state.tasks) == 2:
            break
    card = update_activity(CardState(platform="discord"), state)
    assert card.tool_lines[0] == "⏳ 2 helpers: 2 working"


def test_only_helpers_started_in_this_turn_count() -> None:
    """An archive of an earlier turn's helper emits `terminated` into a later stream."""
    created = construct_type(
        type_=BetaManagedAgentsStreamSessionEvents,
        value={
            "type": "session.thread_created",
            "id": "sevt_created",
            "session_thread_id": "sthr_now",
            "agent_name": "coder",
            "processed_at": "2026-10-31T12:00:00Z",
        },
    )
    idle = construct_type(
        type_=BetaManagedAgentsStreamSessionEvents,
        value={
            "type": "session.thread_status_idle",
            "id": "sevt_idle",
            "session_thread_id": "sthr_now",
            "agent_name": "coder",
            "stop_reason": {"type": "end_turn"},
            "processed_at": "2026-10-31T12:00:01Z",
        },
    )

    def terminated(thread: str, event_id: str) -> Any:
        return construct_type(
            type_=BetaManagedAgentsStreamSessionEvents,
            value={
                "type": "session.thread_status_terminated",
                "id": event_id,
                "session_thread_id": thread,
                "agent_name": "coder",
                "processed_at": "2026-10-31T12:00:02Z",
            },
        )

    state = TurnState()
    for event in (
        created,
        idle,
        terminated("sthr_earlier", "sevt_t1"),
        terminated("sthr_now", "sevt_t2"),
    ):
        state = apply(state, event)
    assert state.tasks == [Task(thread_id="sthr_now", agent_name="coder", status="done")]


def test_the_helper_line_counts_each_state() -> None:
    def helper(n: int, status: Any) -> Task:
        return Task(thread_id=f"t{n}", agent_name="coder", status=status)

    assert format_helper_line(()) is None
    assert format_helper_line((helper(1, "working"),)) == "⏳ 1 helper: 1 working"
    assert (
        format_helper_line((helper(1, "done"), helper(2, "stopped")))
        == "🚫 2 helpers: 1 done, 1 stopped"
    )
