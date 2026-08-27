"""Auto-approve behavior, dedup, and no-fresh-ids termination coverage.

Covers plan 19-08's `AutoApprove` posture: the driver answers a
`requires_action` idle with `user.tool_confirmation` `allow` events and keeps
consuming on the SAME open stream (no reopen), dedups per-turn, dedups
across a reconnect, and refuses to spin when MA re-asks for ids already
allowed. The default posture (`RequireApproval`, implicit and explicit) is
regression-pinned as byte-identical to plan 19-08's predecessors.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from anthropic import AsyncAnthropic
from daimon.core.turn import run_turn
from daimon.core.turn.posture import AutoApprove, BillingExempt, RequireApproval
from daimon.core.turn.state import TextBlock
from daimon.testing.turn_fakes import (
    FakeAnthropic,
    RecordingLifecycle,
    YieldEvent,
)

from .conftest import make_agent_message, make_end_turn, make_requires_action, make_status_idle

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)
_EXEMPT = BillingExempt(reason="cli-operator-run")


def _now() -> datetime:
    return _FROZEN_NOW


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


async def test_auto_approve_confirms_blocked_ids_and_continues_on_same_stream() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1", "tu_2"]),
                )
            ),
            YieldEvent(make_agent_message(event_id="sevt_2", text="all done")),
            YieldEvent(make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())),
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
        billing=_EXEMPT,
        tool_confirmation=AutoApprove(),
    )

    assert final.error is None
    assert final.stop_reason is not None
    assert final.stop_reason.type == "end_turn"
    assert final.content == [TextBlock(kind="text", text="all done")]
    assert fa.beta.sessions.events.stream_calls == 1, "must not reopen the stream"
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]),
        (
            "sess_1",
            [
                {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"},
                {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_2"},
            ],
        ),
    ]
    assert len(lc.terminal_success) == 1


async def test_auto_approve_dedups_overlapping_ids_across_consecutive_idles() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1", "tu_2"]),
                )
            ),
            YieldEvent(
                make_status_idle(
                    event_id="sevt_2",
                    stop_reason=make_requires_action(event_ids=["tu_2", "tu_3"]),
                )
            ),
            YieldEvent(make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())),
        ]
    ]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=AutoApprove(),
    )

    assert final.error is None
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]),
        (
            "sess_1",
            [
                {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"},
                {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_2"},
            ],
        ),
        (
            "sess_1",
            [
                {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_3"},
            ],
        ),
    ], "the second confirmation payload must only carry tu_3 -- tu_2 was already confirmed"


async def test_auto_approve_ends_turn_when_all_requested_ids_already_confirmed() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1"]),
                )
            ),
            YieldEvent(
                make_status_idle(
                    event_id="sevt_2",
                    stop_reason=make_requires_action(event_ids=["tu_1"]),
                )
            ),
        ]
    ]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=AutoApprove(),
    )

    assert final.error is not None
    assert final.error.kind == "requires_action"
    # One entry for the initial user.message, one for the first (only)
    # confirmation batch -- no second confirmation sent for the repeat.
    assert len(fa.beta.sessions.events.sent_events) == 2


async def test_auto_approve_dedups_across_a_reconnect() -> None:
    """The confirmed-id set survives a reconnect: a clean close while
    `running` reopens the stream, and the reopened stream re-delivering the
    SAME `requires_action` idle must not trigger a second send."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="hello")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1", "tu_2"])
    )
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre), YieldEvent(ra_idle)],  # confirms, then exhausts -> clean close
        [YieldEvent(ra_idle)],  # reopened stream re-delivers the SAME idle live
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle]
    fa.beta.sessions.retrieve_statuses = ["running"]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=AutoApprove(),
    )

    assert fa.beta.sessions.events.stream_calls == 2
    # One entry for the initial user.message, one for the first (only)
    # confirmation batch -- the reopened stream re-delivering the same
    # requires_action idle must not re-send a confirmation for ids already
    # confirmed before the reconnect.
    assert len(fa.beta.sessions.events.sent_events) == 2
    # No fresh ids on the reopened stream's idle -> exhausted -> actionable failure.
    assert final.error is not None
    assert final.error.kind == "requires_action"


async def test_default_posture_still_ends_requires_action_turn_as_failure_and_sends_nothing() -> (
    None
):
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1"]),
                )
            )
        ]
    ]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        # no tool_confirmation kwarg -- exercises the default
    )

    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    ], "no confirmation payload should ever be sent under RequireApproval"


async def test_explicit_require_approval_behaves_identically_to_default() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_1",
                    stop_reason=make_requires_action(event_ids=["tu_1"]),
                )
            )
        ]
    ]

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=RequireApproval(),
    )

    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    ], "no confirmation payload should ever be sent under RequireApproval"
