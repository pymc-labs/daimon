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
    RaiseReadTimeout,
    RaiseStreamDrop,
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


async def test_auto_approve_confirms_a_requires_action_discovered_via_a_clean_close_and_finishes_after_reconnect() -> (
    None
):
    """Gap regression: a `requires_action` idle whose event is lost to a
    clean close (never delivered live) must still be confirmed and the turn
    finished, via the eventless-cycle idle branch rather than the live
    consume loop's own AutoApprove check."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="working")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    done = make_status_idle(event_id="sevt_3", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close; ra_idle never delivered live
        [YieldEvent(done)],  # reopened stream after the confirmation is sent
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle]
    fa.beta.sessions.retrieve_statuses = ["idle"]
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

    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]),
        (
            "sess_1",
            [{"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"}],
        ),
    ], "the eventless-cycle idle branch must confirm tu_1 exactly once"
    assert fa.beta.sessions.events.stream_calls == 2, "confirming reopens the stream"
    assert final.error is None
    assert len(lc.terminal_success) == 1


async def test_auto_approve_confirms_a_requires_action_discovered_via_a_read_timeout() -> None:
    """Same shape as the clean-close regression test, but the eventless
    cycle is triggered by a read timeout instead -- both cycle reasons must
    take the identical confirm-and-reopen path."""
    fa = FakeAnthropic()
    ra_idle = make_status_idle(
        event_id="sevt_1", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [RaiseReadTimeout()],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [ra_idle]
    fa.beta.sessions.retrieve_statuses = ["idle"]
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

    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]),
        (
            "sess_1",
            [{"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"}],
        ),
    ], "a read-timeout eventless cycle must take the same confirm-and-reopen path as a clean close"
    assert fa.beta.sessions.events.stream_calls == 2
    assert final.error is None
    assert len(lc.terminal_success) == 1


async def test_auto_approve_does_not_re_confirm_on_the_idle_branch_ids_already_confirmed_live() -> (
    None
):
    """An id confirmed by the LIVE consume loop, then re-seen on the
    eventless-cycle idle branch's replay fold, must not be re-confirmed --
    the per-turn dedup set spans both call sites."""
    fa = FakeAnthropic()
    ra_idle = make_status_idle(
        event_id="sevt_1", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(ra_idle)],  # confirms tu_1 live, then exhausts -> clean close
    ]
    fa.beta.sessions.events.replay_events = [ra_idle]
    fa.beta.sessions.retrieve_statuses = ["idle"]

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

    assert fa.beta.sessions.events.stream_calls == 1, "no fresh ids on the idle branch -> no reopen"
    assert len(fa.beta.sessions.events.sent_events) == 2, (
        "one user.message plus exactly one confirmation batch -- the live confirm, not a second"
    )
    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert "already" in final.error.message, "these ids really were confirmed, live"


async def test_auto_approve_never_sends_confirmations_into_a_terminated_session() -> None:
    """A `terminated` session paused on `requires_action` can no longer
    accept events -- the eventless-cycle branch must never send into it,
    and the finalizer must not claim a confirmation that never happened."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="working")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close; ra_idle never delivered live
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle]
    fa.beta.sessions.retrieve_statuses = ["terminated"]

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

    assert fa.beta.sessions.events.stream_calls == 1
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    ], "a terminated session must never be sent confirmations"
    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert "already" not in final.error.message, (
        "nothing was ever sent for these ids, so the message must not claim they were"
    )


async def test_require_approval_still_finalizes_a_requires_action_found_on_the_idle_branch() -> (
    None
):
    """The default/interactive posture through the same script as the
    clean-close regression test: RequireApproval never sends confirmations,
    and its finalize wording is unchanged by this plan."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="working")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle]
    fa.beta.sessions.retrieve_statuses = ["idle"]

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

    assert fa.beta.sessions.events.stream_calls == 1
    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    ], "no confirmation payload should ever be sent under RequireApproval"
    assert final.error is not None
    assert final.error.kind == "requires_action"
    assert "not supported on this surface" in final.error.message


async def test_auto_approve_eventless_reconnect_keeps_pre_approval_content_when_an_event_follows_the_pause() -> (
    None
):
    """Boundary regression: a `requires_action` idle is a mid-turn PAUSE, so an
    event after it in the replay must not be mistaken for the start of a new
    turn. The prior shape of this test (idle as the LAST replay element) is the
    one case the old scan accidentally handled; here the pause is followed by
    agent content, which the old scan sliced the pause away for -- yielding a
    fold with `stop_reason is None`, no confirmation sent, and a quiet
    truncated success."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="pre-approval text ")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    post = make_agent_message(event_id="sevt_3", text="post-approval text")
    done = make_status_idle(event_id="sevt_4", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close; the pause never delivered live
        [YieldEvent(done)],  # reopened stream after the confirmation is sent
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle, post]
    fa.beta.sessions.retrieve_statuses = ["idle"]
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

    assert fa.beta.sessions.events.sent_events == [
        ("sess_1", [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]),
        (
            "sess_1",
            [{"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"}],
        ),
    ], "the pause must still be found and confirmed when replay content follows it"
    assert fa.beta.sessions.events.stream_calls == 2, "confirming reopens the stream"
    assert final.error is None
    assert final.content == [TextBlock(kind="text", text="pre-approval text post-approval text")], (
        "the fold must keep pre-approval content, not restart at the event after the pause"
    )
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


async def test_auto_approve_mid_turn_reconnect_keeps_pre_approval_content_across_the_pause() -> (
    None
):
    """Same boundary defect on the other call site: the mid-turn `is_retry`
    re-fold, reached through a real connection drop rather than an eventless
    cycle. The turn completes and the confirmation is sent either way -- what
    the old scan lost was the pre-approval text, silently, inside an apparent
    success."""
    fa = FakeAnthropic()
    pre = make_agent_message(event_id="sevt_1", text="pre-approval text ")
    ra_idle = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    post = make_agent_message(event_id="sevt_3", text="post-approval text")
    done = make_status_idle(event_id="sevt_4", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        # confirms tu_1 live, keeps consuming on the same stream, then drops
        [YieldEvent(pre), YieldEvent(ra_idle), RaiseStreamDrop()],
        [YieldEvent(done)],  # reconnect: replay + re-fold happens before this
    ]
    fa.beta.sessions.events.replay_events = [pre, ra_idle, post]
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

    assert lc.reconnects == ["connection_dropped"], "the drop must take the is_retry re-fold path"
    assert final.error is None
    assert final.content == [TextBlock(kind="text", text="pre-approval text post-approval text")], (
        "the re-fold must not slice the turn at its own mid-turn pause"
    )
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(lc.terminal_success) == 1


async def test_auto_approve_reconnect_still_drops_a_prior_turns_content_at_its_end_turn_idle() -> (
    None
):
    """The `requires_action` exemption must not make the scan boundary-blind: a
    prior turn ending in a real `end_turn` idle is still a boundary, so its
    content stays out of the current turn's fold."""
    fa = FakeAnthropic()
    prior = make_agent_message(event_id="sevt_1", text="PRIOR TURN")
    prior_done = make_status_idle(event_id="sevt_2", stop_reason=make_end_turn())
    pre = make_agent_message(event_id="sevt_3", text="pre-approval text ")
    ra_idle = make_status_idle(
        event_id="sevt_4", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    post = make_agent_message(event_id="sevt_5", text="post-approval text")
    done = make_status_idle(event_id="sevt_6", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [prior, prior_done, pre, ra_idle, post]
    fa.beta.sessions.retrieve_statuses = ["idle"]

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
    assert final.content == [TextBlock(kind="text", text="pre-approval text post-approval text")], (
        "the prior turn's content must not leak into the current turn's fold"
    )


async def test_require_approval_treats_a_prior_turns_requires_action_idle_as_a_boundary_on_the_idle_branch() -> (
    None
):
    """Posture regression: the `requires_action` pause exemption applies to
    `AutoApprove` ONLY.

    Under `RequireApproval` the driver cannot answer a `requires_action` idle,
    so `_finalize_success_or_error` ends the turn on it. Nothing retires the MA
    session, so the NEXT turn in the same thread replays that idle. Exempting
    it there folds the prior turn's content -- and its `requires_action` stop
    reason -- into the current turn, which then gets blamed for an approval
    request that belongs to a turn that already ended and already reported it.
    """
    fa = FakeAnthropic()
    prior = make_agent_message(event_id="sevt_1", text="TURN-1 TEXT ")
    prior_end = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    current = make_agent_message(event_id="sevt_3", text="turn-2 text ")
    tail = make_agent_message(event_id="sevt_4", text="tail")
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(current)]]  # clean close
    fa.beta.sessions.events.replay_events = [prior, prior_end, current, tail]
    fa.beta.sessions.retrieve_statuses = ["idle"]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_reused",
        user_message="second question",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=RequireApproval(),
    )

    assert final.content == [TextBlock(kind="text", text="turn-2 text tail")], (
        "the prior turn's text must not be glued onto this turn's answer"
    )
    assert final.stop_reason is None, (
        "the prior turn's requires_action stop reason must not be inherited"
    )
    assert lc.terminal_failures == [], (
        "this turn asked for no tool approval and must not be failed for one"
    )


async def test_require_approval_treats_a_prior_turns_requires_action_idle_as_a_boundary_on_the_retry_refold() -> (
    None
):
    """The same posture regression on the other call site: the mid-turn
    `is_retry` re-fold after a real connection drop."""
    fa = FakeAnthropic()
    prior = make_agent_message(event_id="sevt_1", text="PRIOR TURN TEXT ")
    prior_end = make_status_idle(
        event_id="sevt_2", stop_reason=make_requires_action(event_ids=["tu_1"])
    )
    current = make_agent_message(event_id="sevt_3", text="current turn ")
    done = make_status_idle(event_id="sevt_4", stop_reason=make_end_turn())
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(current), RaiseStreamDrop()],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [prior, prior_end, current]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_reused",
        user_message="second question",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        tool_confirmation=RequireApproval(),
    )

    assert lc.reconnects == ["connection_dropped"]
    assert final.error is None
    assert final.content == [TextBlock(kind="text", text="current turn ")], (
        "the re-fold must still cut at a prior turn that ENDED on requires_action"
    )
