"""`PolicyApproval`: per-call answers to a `requires_action` idle.

The two exit checks for attached-tool write safety live here at the driver
level: an unattended (routine) run refuses a third-party write and still lets
reads through, and a chat turn holds a write until the requester answers the
confirmation card, sending `allow` only after the answer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    ToolSafetyPolicy,
)
from daimon.core.turn import run_turn
from daimon.core.turn.approvals import (
    chat_tool_confirmation,
    headless_tool_confirmation,
    interactive_decider,
    unattended_decider,
)
from daimon.core.turn.posture import (
    AutoApprove,
    BillingExempt,
    PolicyApproval,
    RequireApproval,
)
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_mcp_tool_use,
    make_requires_action,
    make_status_idle,
    make_tool_confirmation,
)

_EXEMPT = BillingExempt(reason="cli-operator-run")
_ON = ToolSafetyPolicy(enabled=True)
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


def _script(fa: FakeAnthropic, *tool_uses: tuple[str, str, str]) -> None:
    """One stream: the tool uses, a pause on all of them, then a finished turn."""
    events: list[YieldEvent] = [
        YieldEvent(
            make_mcp_tool_use(
                event_id=tu_id, name=tool, mcp_server_name=server, input={"title": "Bug"}
            )
        )
        for tu_id, server, tool in tool_uses
    ]
    events.append(
        YieldEvent(
            make_status_idle(
                event_id="sevt_pause",
                stop_reason=make_requires_action(event_ids=[t[0] for t in tool_uses]),
            )
        )
    )
    events.append(YieldEvent(make_agent_message(event_id="sevt_msg", text="done")))
    events.append(YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())))
    fa.beta.sessions.events.stream_scripts = [events]


def _confirmations(fa: FakeAnthropic) -> list[dict[str, object]]:
    return [
        event
        for _session, batch in fa.beta.sessions.events.sent_events
        for event in batch
        if event["type"] == "user.tool_confirmation"
    ]


async def test_routine_write_is_denied_and_read_is_allowed() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_read", "linear", "get_issue"), ("tu_write", "linear", "create_issue"))

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="nightly triage",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=headless_tool_confirmation(_ON),
    )

    assert final.error is None
    sent = _confirmations(fa)
    assert sent[0] == {
        "type": "user.tool_confirmation",
        "result": "allow",
        "tool_use_id": "tu_read",
    }
    assert sent[1]["result"] == "deny"
    assert sent[1]["tool_use_id"] == "tu_write"
    assert "linear/create_issue" in str(sent[1]["deny_message"])
    assert len(sent) == 2


async def test_routine_write_runs_when_the_operator_allowed_it_unattended() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))
    policy = ToolSafetyPolicy(enabled=True, unattended_writes=("linear/create_issue",))

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="nightly triage",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(decide=unattended_decider(policy)),
    )

    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_write"}
    ]


async def test_chat_write_shows_a_card_and_runs_only_after_confirm() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))
    prompts: list[ConfirmationPrompt] = []
    clicked = asyncio.Event()
    sent_before_click: list[dict[str, object]] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        await clicked.wait()
        sent_before_click.extend(_confirmations(fa))
        return "approved"

    decide = interactive_decider(
        _ON, requester_platform_user_id="U1", confirm=card, now=lambda: _NOW
    )
    turn = asyncio.create_task(
        run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=decide),
        )
    )
    for _ in range(200):
        if prompts:
            break
        await asyncio.sleep(0.005)

    assert len(prompts) == 1, "the write must surface as one card"
    prompt = prompts[0]
    assert prompt.title == 'Create issue "Bug"?'
    assert "Title: Bug" in prompt.detail_lines
    assert prompt.requester_platform_user_id == "U1"
    assert not turn.done(), "the turn waits on the card"
    assert _confirmations(fa) == [], "nothing is confirmed before the click"

    clicked.set()
    final = await turn

    assert sent_before_click == []
    assert final.error is None
    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_write"}
    ]


async def test_chat_write_denied_on_the_card_is_refused() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "hubspot", "update_deal"))

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        return "denied"

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="close the deal",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
    )

    (sent,) = _confirmations(fa)
    assert sent["result"] == "deny"
    assert "denied" in str(sent["deny_message"])


async def test_chat_read_runs_without_a_card() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_read", "linear", "list_teams"))

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        raise AssertionError("a read must not post a card")

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="which teams?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
    )

    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_read"}
    ]


async def test_a_surface_without_cards_refuses_writes() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="file a bug",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            _ON, requester_platform_user_id="U1", confirm=None
        ),
    )

    (sent,) = _confirmations(fa)
    assert sent["result"] == "deny"


async def test_a_card_that_fails_to_post_refuses_the_write() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        raise RuntimeError("chat API down")

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="file a bug",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
    )

    (sent,) = _confirmations(fa)
    assert sent["result"] == "deny"


async def test_cancel_while_the_card_is_up_refuses_the_write() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))
    # The interrupt path reattaches to wait for the session to acknowledge.
    fa.beta.sessions.events.stream_scripts.append(
        [YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))]
    )
    cancel = asyncio.Event()
    card_up = asyncio.Event()

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        card_up.set()
        await asyncio.Event().wait()  # nobody ever clicks
        return "approved"

    turn = asyncio.create_task(
        run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            render_interval_s=0.001,
            interrupt_timeout_s=0.05,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(
                decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
            ),
        )
    )
    await asyncio.wait_for(card_up.wait(), timeout=2)
    cancel.set()
    await asyncio.wait_for(turn, timeout=5)

    sent = _confirmations(fa)
    assert [(e["tool_use_id"], e["result"]) for e in sent] == [("tu_write", "deny")]
    types = [ev["type"] for _sid, batch in fa.beta.sessions.events.sent_events for ev in batch]
    assert types.index("user.tool_confirmation") < types.index("user.interrupt")


def test_disabled_policy_keeps_the_old_postures() -> None:
    assert isinstance(headless_tool_confirmation(OPEN_TOOL_SAFETY), AutoApprove)
    assert isinstance(
        chat_tool_confirmation(OPEN_TOOL_SAFETY, requester_platform_user_id="U1", confirm=None),
        RequireApproval,
    )


def test_unattended_chat_turn_gets_the_unattended_rules() -> None:
    posture = chat_tool_confirmation(
        _ON, requester_platform_user_id="U1", confirm=None, attended=False
    )
    assert isinstance(posture, PolicyApproval)


async def test_with_safety_off_asking_before_publishing_runs_no_other_held_call() -> None:
    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="U1",
        confirm=None,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        asks_before_publishing=True,
    )
    assert isinstance(posture, PolicyApproval)
    held = await posture.decide(ToolCall(tool_use_id="t1", server_name="linear", tool_name="x"))
    assert not held.allow, "a tool the agent's own definition holds still never runs"
    publish = ToolCall(tool_use_id="t2", server_name=DAIMON_SERVER_NAME, tool_name="publish_report")
    assert not (await posture.decide(publish)).allow, "no card to press: the publish is refused"
    unseen = await posture.decide(
        ToolCall(tool_use_id="t3", server_name="unknown", tool_name="unknown")
    )
    assert not unseen.allow
    assert "unknown/unknown" not in str(unseen.deny_message), "names no fake tool to the agent"
    assert "call the tool again" in str(unseen.deny_message)


def _replay_script(fa: FakeAnthropic) -> None:
    """The pause is lost to a clean close and only found in the replay."""
    pre = make_mcp_tool_use(
        event_id="tu_write", name="delete_issue", mcp_server_name="linear", input={"id": "ENG-1"}
    )
    pause = make_status_idle(
        event_id="sevt_pause", stop_reason=make_requires_action(event_ids=["tu_write"])
    )
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(pre)],  # exhausts -> clean close; the pause never arrives live
        [YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn()))],
    ]
    fa.beta.sessions.events.replay_events = [pre, pause]
    fa.beta.sessions.retrieve_statuses = ["idle"]


async def test_replayed_pause_is_decided_through_the_card() -> None:
    fa = FakeAnthropic()
    _replay_script(fa)

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        return "approved"

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="delete it",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
    )

    assert final.error is None
    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_write"}
    ]


async def _cancel_replayed_pause(
    *, click_after_s: float | None
) -> tuple[FakeAnthropic, list[bool]]:
    fa = FakeAnthropic()
    _replay_script(fa)
    fa.beta.sessions.events.stream_scripts[1] = [
        YieldEvent(make_status_idle(event_id="ack", stop_reason=make_end_turn()))
    ]
    cancel = asyncio.Event()
    card_up = asyncio.Event()
    clicked = asyncio.Event()
    retired: list[bool] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        card_up.set()
        try:
            await clicked.wait()
        except asyncio.CancelledError:
            retired.append(True)
            raise
        return "approved"

    turn = asyncio.create_task(
        run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="delete it",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            render_interval_s=0.001,
            interrupt_timeout_s=0.05,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(
                decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
            ),
        )
    )
    await asyncio.wait_for(card_up.wait(), timeout=2)
    cancel.set()
    if click_after_s is None:
        clicked.set()  # Approve lands in the same tick as Stop
    else:
        await asyncio.sleep(click_after_s)
        clicked.set()
    await asyncio.wait_for(turn, timeout=5)
    return fa, retired


async def test_cancel_on_a_replayed_pause_never_sends_allow_and_retires_the_card() -> None:
    """Review regression: the eventless/replay path used to await the decision
    without racing cancel, so an Approve clicked after Stop still sent allow."""
    fa, retired = await _cancel_replayed_pause(click_after_s=0.05)

    assert [(e["tool_use_id"], e["result"]) for e in _confirmations(fa)] == [("tu_write", "deny")]
    assert retired == [True], "the card is cancelled so its hook can retire it"


async def test_an_approve_in_the_same_tick_as_stop_is_still_refused() -> None:
    fa, _retired = await _cancel_replayed_pause(click_after_s=None)

    assert [(e["tool_use_id"], e["result"]) for e in _confirmations(fa)] == [("tu_write", "deny")]


async def test_a_turn_deadline_while_a_card_is_up_leaves_no_decide_task_behind() -> None:
    """Review regression: a ceiling breach cancelled the driver but left
    `turn.decide_blocked` (and its card) waiting forever."""
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))
    retired: list[bool] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        try:
            await asyncio.Event().wait()  # nobody clicks
        except asyncio.CancelledError:
            retired.append(True)
            raise
        return "approved"

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="file a bug",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
        deadline=datetime.now(UTC) + timedelta(milliseconds=200),
    )
    await asyncio.sleep(0)

    assert final.error is not None and final.error.kind == "ceiling"
    assert retired == [True]
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "turn.decide_blocked"]
    assert [(e["tool_use_id"], e["result"]) for e in _confirmations(fa)] == [
        ("tu_write", "deny")
    ], "a ceiling breach refuses the pending call instead of leaving the session paused"


async def test_trusted_daimon_server_is_not_gated_and_an_untrusted_one_is() -> None:
    fa = FakeAnthropic()
    _script(fa, ("tu_daimon", "daimon-mcp", "routine_delete"))

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="tidy",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(decide=unattended_decider(_ON)),  # no trust set
    )

    (sent,) = _confirmations(fa)
    assert sent["result"] == "deny", "without a verified endpoint the reserved name earns nothing"


async def test_a_blocked_call_nobody_saw_is_refused_without_a_card() -> None:
    """Eval regression: a pause naming an id whose tool_use never reached the
    folded state used to post a blank "unknown" card a person could approve."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_status_idle(
                    event_id="sevt_pause",
                    stop_reason=make_requires_action(event_ids=["tu_unseen"]),
                )
            ),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ]
    ]
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="go",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
        ),
    )

    assert prompts == [], "no card for a call nobody can see"
    (sent,) = _confirmations(fa)
    assert (sent["tool_use_id"], sent["result"]) == ("tu_unseen", "deny")
    assert "could not see" in str(sent["deny_message"])


async def test_a_stalled_denial_send_cannot_hold_the_turn_past_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review regression (round 2): cleanup after a ceiling breach awaited the
    best-effort deny with no bound, so an MA outage defeated the ceiling."""
    import daimon.core.turn.driver as driver_mod

    monkeypatch.setattr(driver_mod, "CLEANUP_BUDGET_S", 0.2)
    fa = FakeAnthropic()
    _script(fa, ("tu_write", "linear", "create_issue"))
    real_send = fa.beta.sessions.events.send

    async def stalled_send(session_id: str, *, events: list[dict[str, object]]) -> None:
        if any(e["type"] == "user.tool_confirmation" for e in events):
            await asyncio.Event().wait()  # MA never answers
        await real_send(session_id, events=events)

    fa.beta.sessions.events.send = stalled_send  # type: ignore[method-assign]

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        await asyncio.Event().wait()  # nobody clicks
        return "approved"

    started = asyncio.get_running_loop().time()
    final = await asyncio.wait_for(
        run_turn(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(
                decide=interactive_decider(_ON, requester_platform_user_id="U1", confirm=card)
            ),
            deadline=datetime.now(UTC) + timedelta(milliseconds=100),
        ),
        timeout=5,
    )
    elapsed = asyncio.get_running_loop().time() - started
    await asyncio.sleep(0)

    assert final.error is not None and final.error.kind == "ceiling"
    assert elapsed < 1.5, f"cleanup must stay within its budget, took {elapsed:.2f}s"
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "turn.decide_blocked"]
    assert all(e["result"] != "allow" for e in _confirmations(fa))


async def test_ma_repeating_the_pause_while_the_card_is_up_does_not_end_the_turn() -> None:
    """Production 2026-10-08: MA paused on a publish call, ran the batch's
    other tool, and paused again on the same id while the card was up. The
    repeat, read after Approve, ended the turn as "sent but not accepted"
    though MA then ran the call. It is a duplicate until MA echoes the allow."""
    fa = FakeAnthropic()
    pause = make_requires_action(event_ids=["tu_pub"])
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                make_mcp_tool_use(
                    event_id="tu_pub",
                    name="create_attachment_upload_url",
                    mcp_server_name=DAIMON_SERVER_NAME,
                )
            ),
            YieldEvent(make_status_idle(event_id="sevt_pause_1", stop_reason=pause)),
            YieldEvent(make_status_idle(event_id="sevt_pause_2", stop_reason=pause)),
            YieldEvent(make_tool_confirmation(event_id="sevt_took", tool_use_id="tu_pub")),
            YieldEvent(make_agent_message(event_id="sevt_msg", text="uploaded")),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ]
    ]
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="publish the notebook",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            ToolSafetyPolicy(enabled=False),
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            asks_before_publishing=True,
        ),
    )

    assert final.error is None
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(prompts) == 1, "one card for one call"
    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_pub"}
    ]


@pytest.mark.parametrize(
    ("policy", "asks_before_publishing"),
    [(_ON, False), (OPEN_TOOL_SAFETY, True)],
    ids=["tool-safety-on", "publish-only"],
)
async def test_same_target_uploads_share_one_card_but_send_one_confirmation_per_call(
    policy: ToolSafetyPolicy, asks_before_publishing: bool
) -> None:
    fa = FakeAnthropic()
    names = ["cg_meme.csv", "launch_features.csv", "launch_curves.csv"]
    events = [
        YieldEvent(
            make_mcp_tool_use(
                event_id=f"tu_{index}",
                name="create_attachment_upload_url",
                mcp_server_name=DAIMON_SERVER_NAME,
                input={"name": name, "slug": "memecoin-scan"},
            )
        )
        for index, name in enumerate(names)
    ]
    events.extend(
        [
            YieldEvent(
                make_status_idle(
                    event_id="pause",
                    stop_reason=make_requires_action(
                        event_ids=[f"tu_{index}" for index in range(3)]
                    ),
                )
            ),
            YieldEvent(make_agent_message(event_id="message", text="done")),
            YieldEvent(make_status_idle(event_id="end", stop_reason=make_end_turn())),
        ]
    )
    fa.beta.sessions.events.stream_scripts = [events]
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        assert _confirmations(fa) == []
        return "approved"

    final = await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="upload files",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            policy,
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            asks_before_publishing=asks_before_publishing,
        ),
    )
    assert final.error is None
    assert len(prompts) == 1
    assert prompts[0].title == 'Upload 3 files to notebook "memecoin-scan"?'
    assert prompts[0].items == tuple(names)
    assert [event["tool_use_id"] for event in _confirmations(fa)] == ["tu_0", "tu_1", "tu_2"]
    assert all(event["result"] == "allow" for event in _confirmations(fa))


async def test_publish_only_group_fallback_refuses_other_held_calls() -> None:
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="U1",
        confirm=card,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        asks_before_publishing=True,
    )
    assert isinstance(posture, PolicyApproval) and posture.decide_group is not None
    calls = [
        ToolCall(
            tool_use_id=f"tu_{index}",
            server_name="linear",
            tool_name="create_issue",
            input={"title": "Bug"},
        )
        for index in range(2)
    ]
    decisions = await posture.decide_group(calls)
    assert not any(decision.allow for decision in decisions)
    assert prompts == [], "non-publish held calls cannot gain a card through grouping"


async def test_generic_group_keeps_the_single_call_title() -> None:
    from daimon.core.turn.approvals import interactive_group_decider

    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    decide = interactive_group_decider(_ON, requester_platform_user_id="U1", confirm=card)
    calls = [
        ToolCall(
            tool_use_id=f"tu_{index}",
            server_name="linear",
            tool_name="create_issue",
            input={"title": "Bug"},
        )
        for index in range(2)
    ]
    decisions = await decide(calls)
    assert all(decision.allow for decision in decisions)
    assert len(prompts) == 1
    assert prompts[0].title == 'Create issue "Bug"?'
    assert prompts[0].items == ("Bug", "Bug")


async def test_upload_group_splits_after_five_and_keeps_other_tools_separate() -> None:
    from daimon.core.turn.approvals import interactive_group_decider

    calls = [
        ToolCall(
            tool_use_id=f"upload_{index}",
            server_name=DAIMON_SERVER_NAME,
            tool_name="create_attachment_upload_url",
            input={"name": f"file_{index}.csv", "slug": "memecoin-scan"},
        )
        for index in range(6)
    ]
    calls.append(
        ToolCall(
            tool_use_id="notebook",
            server_name=DAIMON_SERVER_NAME,
            tool_name="create_notebook_upload_url",
            input={"slug": "memecoin-scan"},
        )
    )
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    decide = interactive_group_decider(
        _ON,
        requester_platform_user_id="U1",
        confirm=card,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        now=lambda: _NOW,
    )
    decisions = await decide(calls)
    assert all(decision.allow for decision in decisions)
    assert len(prompts) == 3
    assert [len(prompt.items) for prompt in prompts] == [5, 0, 0]
    assert prompts[0].title == 'Upload 5 files to notebook "memecoin-scan"?'
    assert prompts[1].title == 'Upload "file_5.csv" to notebook "memecoin-scan"?'
    assert prompts[2].title == 'Publish notebook "memecoin-scan"?'
