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
from daimon.core.confirmation import ApprovedConfirmation, ConfirmationAnswer, ConfirmationPrompt
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
)
from daimon.core.turn.approvals import (
    chat_tool_confirmation,
    interactive_decider,
    unattended_decider,
)
from daimon.core.turn.posture import (
    BillingExempt,
    PolicyApproval,
    RequireApproval,
)
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent, run_turn_on_leg

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_mcp_tool_result,
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


def _script(
    fa: FakeAnthropic,
    *tool_uses: tuple[str, str, str],
    inputs: dict[str, dict[str, object]] | None = None,
) -> None:
    """One stream: the tool uses, a pause on all of them, then a finished turn.

    `inputs` gives a tool use's input by its id; the default is a title.
    """
    events: list[YieldEvent] = [
        YieldEvent(
            make_mcp_tool_use(
                event_id=tu_id,
                name=tool,
                mcp_server_name=server,
                input=(inputs or {}).get(tu_id, {"title": "Bug"}),
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

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="nightly triage",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(decide=unattended_decider(_ON)),
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

    await run_turn_on_leg(
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
        run_turn_on_leg(
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

    await run_turn_on_leg(
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

    await run_turn_on_leg(
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

    await run_turn_on_leg(
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

    await run_turn_on_leg(
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
        run_turn_on_leg(
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

    final = await run_turn_on_leg(
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
        run_turn_on_leg(
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

    final = await run_turn_on_leg(
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

    await run_turn_on_leg(
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

    await run_turn_on_leg(
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

    async def stalled_send(session_id: str, *, events: list[dict[str, object]]) -> object:
        if any(e["type"] == "user.tool_confirmation" for e in events):
            await asyncio.Event().wait()  # MA never answers
        return await real_send(session_id, events=events)

    fa.beta.sessions.events.send = stalled_send  # type: ignore[method-assign]

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        await asyncio.Event().wait()  # nobody clicks
        return "approved"

    started = asyncio.get_running_loop().time()
    final = await asyncio.wait_for(
        run_turn_on_leg(
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

    final = await run_turn_on_leg(
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


async def test_ma_taking_three_approvals_one_per_pause_does_not_end_the_turn() -> None:
    """Production 2026-10-08 08:58: three approved uploads in one batch. MA takes
    one confirmation per pause, so after the batch is sent it pauses again on the
    ids it has not run yet. Each such pause is a duplicate, not a re-ask: the turn
    runs all three and ends, and each call gets exactly one card and one allow."""
    fa = FakeAnthropic()
    ids = ["tu_0", "tu_1", "tu_2"]
    events: list[YieldEvent] = [
        YieldEvent(
            make_mcp_tool_use(
                event_id=tool_use_id,
                name="create_attachment_upload_url",
                mcp_server_name=DAIMON_SERVER_NAME,
                input={"name": f"{tool_use_id}.csv", "slug": "qa"},
            )
        )
        for tool_use_id in ids
    ]
    for index, tool_use_id in enumerate(ids):
        events.append(
            YieldEvent(
                make_status_idle(
                    event_id=f"sevt_pause_{index}",
                    stop_reason=make_requires_action(event_ids=ids[index:]),
                )
            )
        )
        events.append(
            YieldEvent(
                make_tool_confirmation(event_id=f"sevt_took_{index}", tool_use_id=tool_use_id)
            )
        )
        events.append(
            YieldEvent(
                make_mcp_tool_result(event_id=f"sevt_result_{index}", mcp_tool_use_id=tool_use_id)
            )
        )
    events.append(YieldEvent(make_agent_message(event_id="sevt_msg", text="uploaded all three")))
    events.append(YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())))
    fa.beta.sessions.events.stream_scripts = [events]
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="upload the three files",
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

    assert final.error is None, "not abandoned as 'sent but not accepted'"
    assert final.stop_reason is not None and final.stop_reason.type == "end_turn"
    assert len(prompts) == 3, "one card per call, never re-shown"
    assert sorted(c["tool_use_id"] for c in _confirmations(fa)) == ids
    assert all(c["result"] == "allow" for c in _confirmations(fa))


async def test_three_uploads_get_separate_cards_and_answers() -> None:
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
        return "denied" if "launch_features.csv" in prompt.title else "approved"

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="upload files",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            OPEN_TOOL_SAFETY,
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            asks_before_publishing=True,
        ),
    )
    assert final.error is None
    assert len(prompts) == 3
    assert {prompt.title for prompt in prompts} == {
        f'Upload "{name}" to notebook "memecoin-scan"?' for name in names
    }
    assert [event["tool_use_id"] for event in _confirmations(fa)] == ["tu_0", "tu_1", "tu_2"]
    assert [event["result"] for event in _confirmations(fa)] == ["allow", "deny", "allow"]


async def test_stop_retires_an_approved_card_before_its_allow_is_sent() -> None:
    fa = FakeAnthropic()
    events = [
        YieldEvent(
            make_mcp_tool_use(
                event_id=f"tu_{title.lower()}",
                name="create_issue",
                mcp_server_name="linear",
                input={"title": title},
            )
        )
        for title in ("First", "Second")
    ]
    events.append(
        YieldEvent(
            make_status_idle(
                event_id="pause",
                stop_reason=make_requires_action(event_ids=["tu_first", "tu_second"]),
            )
        )
    )
    fa.beta.sessions.events.stream_scripts = [
        events,
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    second_open = asyncio.Event()
    first_state = "Pending"
    second_stopped = False

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
        nonlocal first_state, second_stopped
        if "First" in prompt.title:
            first_state = "Approved"

            async def retire_unsent() -> None:
                nonlocal first_state
                first_state = "Stopped"

            return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
        second_open.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            second_stopped = True
            raise
        return "approved"

    turn = asyncio.create_task(
        run_turn_on_leg(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="create two issues",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=chat_tool_confirmation(
                _ON,
                requester_platform_user_id="U1",
                confirm=card,
            ),
        )
    )
    await asyncio.wait_for(second_open.wait(), timeout=2)
    await asyncio.sleep(0)
    assert first_state == "Approved" and _confirmations(fa) == []
    cancel.set()
    await asyncio.wait_for(turn, timeout=5)

    assert first_state == "Stopped"
    assert second_stopped
    assert [(event["tool_use_id"], event["result"]) for event in _confirmations(fa)] == [
        ("tu_first", "deny"),
        ("tu_second", "deny"),
    ]


async def test_the_card_expiry_follows_the_operator_setting() -> None:
    """Staging sets a short approval expiry; production keeps ten minutes."""
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    short = ToolSafetyPolicy(enabled=True, confirmation_timeout_s=60)
    decide = interactive_decider(
        short, requester_platform_user_id="U1", confirm=card, now=lambda: _NOW
    )
    await decide(ToolCall(tool_use_id="t1", server_name="linear", tool_name="create_issue"))
    assert prompts[0].expires_at == _NOW + timedelta(seconds=60)

    default = interactive_decider(
        _ON, requester_platform_user_id="U1", confirm=card, now=lambda: _NOW
    )
    await default(ToolCall(tool_use_id="t2", server_name="linear", tool_name="create_issue"))
    assert prompts[1].expires_at == _NOW + timedelta(minutes=10)


async def test_the_card_expiry_setting_reaches_the_publish_only_path() -> None:
    """Production runs with tool safety off and asks only before publishing;
    the expiry setting must reach that path too (review of #517)."""
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    posture = chat_tool_confirmation(
        ToolSafetyPolicy(enabled=False, confirmation_timeout_s=90),
        requester_platform_user_id="U1",
        confirm=card,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        asks_before_publishing=True,
    )
    assert isinstance(posture, PolicyApproval)
    before = datetime.now(UTC)
    await posture.decide(
        ToolCall(tool_use_id="t1", server_name=DAIMON_SERVER_NAME, tool_name="publish_report")
    )
    assert len(prompts) == 1
    lifetime = prompts[0].expires_at - before
    assert timedelta(seconds=85) <= lifetime <= timedelta(seconds=95)


def _proxied(inner: str, **arguments: object) -> dict[str, object]:
    return {"name": inner, "arguments": arguments}


async def test_a_denied_daimon_tool_behind_call_tool_is_refused_and_an_unruled_one_runs() -> None:
    fa = FakeAnthropic()
    _script(
        fa,
        ("tu_denied", DAIMON_SERVER_NAME, "call_tool"),
        ("tu_free", DAIMON_SERVER_NAME, "call_tool"),
        inputs={
            "tu_denied": _proxied("hubspot_update_objects", title="Renewal"),
            "tu_free": _proxied("get_session"),
        },
    )
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    looked_up: list[str] = []
    prompts: list[ConfirmationPrompt] = []

    async def rules_for(call: ToolCall) -> ResolvedAgentToolRule | None:
        looked_up.append(call.key)
        return (
            ResolvedAgentToolRule(rule="deny")
            if call.tool_name == "hubspot_update_objects"
            else None
        )

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    final = await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="update the deal",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            policy,
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            daimon_rules=True,
            rules_for=rules_for,
        ),
    )

    assert final.error is None
    sent = _confirmations(fa)
    assert [(e["tool_use_id"], e["result"]) for e in sent] == [
        ("tu_denied", "deny"),
        ("tu_free", "allow"),
    ]
    assert "daimon-mcp/hubspot_update_objects" in str(sent[0]["deny_message"])
    assert looked_up == [
        "daimon-mcp/hubspot_update_objects",
        "daimon-mcp/get_session",
    ], "the rule is looked up under the tool inside call_tool, not call_tool"
    assert prompts == [], "no card for a refusal or for a call with no rule"


async def test_an_ask_rule_on_a_daimon_tool_behind_call_tool_waits_for_the_card() -> None:
    fa = FakeAnthropic()
    _script(
        fa,
        ("tu_ask", DAIMON_SERVER_NAME, "call_tool"),
        inputs={"tu_ask": _proxied("hubspot_update_objects", title="Renewal")},
    )
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    prompts: list[ConfirmationPrompt] = []
    clicked = asyncio.Event()

    async def rules_for(_call: ToolCall) -> ResolvedAgentToolRule | None:
        return ResolvedAgentToolRule(rule="ask")

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        await clicked.wait()
        return "approved"

    turn = asyncio.create_task(
        run_turn_on_leg(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="update the deal",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=chat_tool_confirmation(
                policy,
                requester_platform_user_id="U1",
                confirm=card,
                trusted_servers=frozenset({DAIMON_SERVER_NAME}),
                daimon_rules=True,
                rules_for=rules_for,
            ),
        )
    )
    for _ in range(200):
        if prompts:
            break
        await asyncio.sleep(0.005)

    assert [p.title for p in prompts] == ['Hubspot update objects "Renewal"?']
    assert _confirmations(fa) == [], "nothing is confirmed before the click"
    clicked.set()
    final = await turn
    assert final.error is None
    assert _confirmations(fa) == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_ask"}
    ]


async def test_an_unattended_run_refuses_an_ask_ruled_daimon_tool_behind_call_tool() -> None:
    fa = FakeAnthropic()
    _script(
        fa,
        ("tu_ask", DAIMON_SERVER_NAME, "call_tool"),
        inputs={"tu_ask": _proxied("hubspot_update_objects")},
    )

    async def rules_for(_call: ToolCall) -> ResolvedAgentToolRule | None:
        return ResolvedAgentToolRule(rule="ask")

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="nightly",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=PolicyApproval(
            decide=unattended_decider(
                ToolSafetyPolicy(enabled=True, agent_rules=True),
                trusted_servers=frozenset({DAIMON_SERVER_NAME}),
                daimon_rules=True,
                rules_for=rules_for,
            )
        ),
    )

    [sent] = _confirmations(fa)
    assert sent["result"] == "deny" and sent["tool_use_id"] == "tu_ask"
    assert "daimon-mcp/hubspot_update_objects" in str(sent["deny_message"])


async def test_an_agent_without_daimon_rules_runs_a_proxied_call_with_no_lookup_or_card() -> None:
    fa = FakeAnthropic()
    _script(
        fa,
        ("tu_free", DAIMON_SERVER_NAME, "call_tool"),
        inputs={"tu_free": _proxied("get_session")},
    )
    looked_up: list[str] = []
    prompts: list[ConfirmationPrompt] = []

    async def rules_for(call: ToolCall) -> ResolvedAgentToolRule | None:
        looked_up.append(call.key)
        return None

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="look",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            ToolSafetyPolicy(enabled=True, agent_rules=True),
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            rules_for=rules_for,
        ),
    )

    assert [(e["tool_use_id"], e["result"]) for e in _confirmations(fa)] == [("tu_free", "allow")]
    assert looked_up == [] and prompts == []


async def test_an_approval_tool_sent_through_call_tool_is_refused_without_a_card() -> None:
    fa = FakeAnthropic()
    _script(
        fa,
        ("tu_pub", DAIMON_SERVER_NAME, "call_tool"),
        inputs={"tu_pub": _proxied("publish_report", title="Q3")},
    )
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    await run_turn_on_leg(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="publish",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=_EXEMPT,
        tool_confirmation=chat_tool_confirmation(
            ToolSafetyPolicy(enabled=True, agent_rules=True),
            requester_platform_user_id="U1",
            confirm=card,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            daimon_rules=True,
        ),
    )

    [sent] = _confirmations(fa)
    assert sent["result"] == "deny" and "Call publish_report directly" in str(sent["deny_message"])
    assert prompts == []


@pytest.mark.parametrize("fail", [False, True])
def test_unattended_context_restores_nested_gate_and_execution(fail: bool) -> None:
    from daimon.core.turn import mutation_gate
    from daimon.core.turn.driver import unattended_sends

    async def gate() -> None:
        pass

    assert mutation_gate.unattended_gate.get() is None
    assert mutation_gate.unattended_execution.get() is False
    with unattended_sends(gate):
        assert mutation_gate.unattended_gate.get() is gate
        assert mutation_gate.unattended_execution.get() is True
        try:
            with unattended_sends(None):
                assert mutation_gate.unattended_gate.get() is None
                assert mutation_gate.unattended_execution.get() is True
                if fail:
                    raise RuntimeError("refused")
        except RuntimeError:
            assert fail
        assert mutation_gate.unattended_gate.get() is gate
        assert mutation_gate.unattended_execution.get() is True
    assert mutation_gate.unattended_gate.get() is None
    assert mutation_gate.unattended_execution.get() is False


async def test_an_unattended_turn_gates_before_its_message_and_each_confirmation() -> None:
    """Review B, finding 1: one boundary for every unattended send."""
    from daimon.core.turn.driver import unattended_sends

    fa = FakeAnthropic()
    _script(fa, ("tu_read", "linear", "get_issue"))
    order: list[str] = []

    async def gate() -> None:
        order.append(f"gate@{len(fa.beta.sessions.events.sent_events)}")

    with unattended_sends(gate):
        final = await run_turn_on_leg(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="nightly",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=unattended_decider(_ON)),
        )
    assert final.error is None
    # Before the message (nothing sent yet), then before the confirmation.
    assert order == ["gate@0", "gate@1"]
    assert [e["result"] for e in _confirmations(fa)] == ["allow"]
    # Finding 2: every send asked for a client with no SDK retry.
    assert sum(o == {"max_retries": 0} for o in fa.options) == 2
    assert all(o["max_retries"] in {0, 2} for o in fa.options)


async def test_a_failed_confirmation_gate_sends_no_confirmation() -> None:
    from daimon.core.turn.driver import unattended_sends

    fa = FakeAnthropic()
    _script(fa, ("tu_read", "linear", "get_issue"))
    calls = 0

    async def gate() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("session update refused")

    with unattended_sends(gate), pytest.raises(RuntimeError, match="refused"):
        await run_turn_on_leg(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="nightly",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=unattended_decider(_ON)),
        )
    assert not [e for e in _confirmations(fa) if e["result"] == "allow"]


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize(
    "stop", ["decision_cancel", "gate_cancel", "gate_ceiling", "gate_failure", "gate_task_cancel"]
)
async def test_unattended_stops_leave_calls_paused_and_interrupt(replay: bool, stop: str) -> None:
    from daimon.core.turn.driver import unattended_sends
    from daimon.core.turn.posture import ToolConfirmationResult

    fa = FakeAnthropic()
    if replay:
        _replay_script(fa)
    else:
        _script(fa, ("tu_write", "linear", "create_issue"))
    fa.beta.sessions.events.stream_scripts.append(
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))]
    )
    entered = asyncio.Event()
    cancel = asyncio.Event()
    calls = 0
    cancelled_gate = False

    async def gate() -> None:
        nonlocal calls, cancelled_gate
        calls += 1
        if calls == 1:
            return  # Initial message is gated successfully.
        entered.set()
        if stop == "gate_failure":
            raise RuntimeError("gate failed")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_gate = True
            raise

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        if stop == "decision_cancel":
            entered.set()
            await asyncio.Event().wait()
        return ToolConfirmationResult(allow=True)

    async def run():
        with unattended_sends(gate):
            return await run_turn_on_leg(
                anthropic=_cast(fa),
                session_id="sess_1",
                user_message="nightly",
                lifecycle=RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=0.05,
                billing=_EXEMPT,
                tool_confirmation=PolicyApproval(decide=decide),
                deadline=(datetime.now(UTC) + timedelta(seconds=0.3))
                if stop == "gate_ceiling"
                else None,
            )

    turn = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=2)
    if stop in ("decision_cancel", "gate_cancel"):
        cancel.set()
    elif stop == "gate_task_cancel":
        turn.cancel()
    if stop == "gate_failure":
        with pytest.raises(RuntimeError, match="gate failed"):
            await turn
    elif stop == "gate_task_cancel":
        with pytest.raises(asyncio.CancelledError):
            await turn
    else:
        result = await asyncio.wait_for(turn, timeout=3)
        if stop == "gate_ceiling":
            assert result.error is not None and result.error.kind == "ceiling"
    assert _confirmations(fa) == [], (
        "an unsuccessful boundary must send no confirmations, even denials"
    )
    assert any(
        event["type"] == "user.interrupt"
        for _, batch in fa.beta.sessions.events.sent_events
        for event in batch
    ), "stopping a gated boundary interrupts the session"
    if stop in ("gate_cancel", "gate_ceiling", "gate_task_cancel"):
        assert cancelled_gate
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("turn.") and not task.done()
    ]


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("stop", ["stop", "cancel", "ceiling", "normal"])
async def test_ungated_unattended_abort_never_answers_pending_calls(
    replay: bool, stop: str
) -> None:
    from daimon.core.turn.driver import unattended_sends
    from daimon.core.turn.posture import ToolConfirmationResult
    from daimon.core.turn.unattended import refuse_ungated_confirmation

    fa = FakeAnthropic()
    if replay:
        _replay_script(fa)
    else:
        _script(fa, ("tu_write", "linear", "create_issue"))
    fa.beta.sessions.events.stream_scripts.append(
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))]
    )
    entered = asyncio.Event()
    cancel = asyncio.Event()

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        entered.set()
        if stop != "normal":
            await asyncio.Event().wait()
        return await refuse_ungated_confirmation(call)

    async def run():
        # The actual non-gateable prepared context: there is no refresh gate,
        # and the normal decision is always the plain refusal above.
        with unattended_sends(None):
            return await run_turn_on_leg(
                anthropic=_cast(fa),
                session_id="sess_1",
                user_message="wake up",
                lifecycle=RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=0.05,
                billing=_EXEMPT,
                tool_confirmation=PolicyApproval(decide=decide),
                deadline=datetime.now(UTC) + timedelta(seconds=0.3) if stop == "ceiling" else None,
            )

    turn = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=2)
    if stop == "stop":
        cancel.set()
    elif stop == "cancel":
        turn.cancel()
    if stop == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await turn
    else:
        state = await asyncio.wait_for(turn, timeout=3)
        if stop == "ceiling":
            assert state.error is not None and state.error.kind == "ceiling"
    if stop == "normal":
        confirmations = _confirmations(fa)
        assert confirmations and all(event["result"] == "deny" for event in confirmations)
        assert all("cannot refresh" in str(event["deny_message"]) for event in confirmations)
    else:
        assert _confirmations(fa) == []
        assert any(
            event["type"] == "user.interrupt"
            for _, batch in fa.beta.sessions.events.sent_events
            for event in batch
        ), "every unattended abort interrupts, whether or not a gate exists"
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("turn.") and not task.done()
    ]


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
async def test_outside_cancel_during_stop_drain_preserves_decider_cleanup(
    monkeypatch: pytest.MonkeyPatch, replay: bool
) -> None:
    from daimon.core.turn import driver
    from daimon.core.turn.posture import ToolConfirmationResult

    fa = FakeAnthropic()
    if replay:
        _replay_script(fa)
    else:
        _script(fa, ("tu_write", "linear", "create_issue"))
    fa.beta.sessions.events.stream_scripts.append(
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))]
    )
    entered = asyncio.Event()
    draining = asyncio.Event()
    abort_joined = asyncio.Event()
    release = asyncio.Event()
    cancel = asyncio.Event()
    retired = False
    original_bounded = driver._bounded

    async def bounded(task: asyncio.Task[object], *, what: str, session_id: str) -> None:
        current = asyncio.current_task()
        if (
            what == "decide_task_join"
            and current is not None
            and current.get_name() == "turn.decision_abort"
        ):
            abort_joined.set()
        await original_bounded(task, what=what, session_id=session_id)

    monkeypatch.setattr(driver, "_bounded", bounded)

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        nonlocal retired
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
            retired = True
            raise
        return ToolConfirmationResult(allow=False)

    async def run() -> None:
        with driver.unattended_sends(None):
            await run_turn_on_leg(
                anthropic=_cast(fa),
                session_id="sess_1",
                user_message="wake up",
                lifecycle=RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=0.05,
                billing=_EXEMPT,
                tool_confirmation=PolicyApproval(decide=decide),
            )

    turn = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=2)
    cancel.set()
    await asyncio.wait_for(draining.wait(), timeout=2)
    turn.cancel()
    await asyncio.wait_for(abort_joined.wait(), timeout=2)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert retired, "outside cancellation must not cancel the decider's cleanup again"
    assert _confirmations(fa) == []
    assert any(
        event["type"] == "user.interrupt"
        for _, batch in fa.beta.sessions.events.sent_events
        for event in batch
    )
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("turn.") and not task.done()
    ]


@pytest.mark.parametrize("cap", ["balance", "member_cap", "channel_budget"])
async def test_watch_confirmation_send_rechecks_each_spend_gate(db_session_factory, cap):
    from decimal import Decimal

    from daimon.core.stores import tenant_ledger
    from daimon.core.turn.admission import require_current_spend
    from daimon.core.turn.driver import unattended_sends
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.posture import ToolConfirmationResult
    from daimon.testing.factories import make_channel_budget, make_tenant, make_tenant_user_cap
    from daimon.testing.turn_fakes import deps_for

    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant.id,
            delta_usd=Decimal(100),
            reason="trial",
            idempotency_key=f"initial:{tenant.id}",
        )
    fa = FakeAnthropic()
    _script(fa, ("tu_read", "linear", "get_issue"))
    fa.beta.sessions.events.stream_scripts.append(
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))]
    )
    deps = deps_for(client=_cast(fa), sm=db_session_factory)
    decisions = []

    async def decide(call):
        # Spend changes after the first model send, while a read awaits its answer.
        async with db_session_factory.begin() as db:
            if cap == "balance":
                await tenant_ledger.insert_entry(
                    db,
                    tenant_id=tenant.id,
                    delta_usd=Decimal(-101),
                    reason="usage",
                    idempotency_key=f"drain:{tenant.id}",
                )
            elif cap == "member_cap":
                await make_tenant_user_cap(db, tenant=tenant, user_id="owner", amount=Decimal(0))
            else:
                await make_channel_budget(
                    db, tenant=tenant, channel_id="budget-channel", limit_usd=Decimal(0)
                )
        decisions.append(call)
        return ToolConfirmationResult(allow=True)

    async def gate():
        await require_current_spend(
            deps,
            tenant_id=tenant.id,
            platform=tenant.platform,
            external_user_id="owner",
            budget_channel_id="budget-channel",
            now=datetime.now(UTC),
        )

    with unattended_sends(gate), pytest.raises(AdmissionDenied):
        await run_turn_on_leg(
            anthropic=_cast(fa),
            session_id="sess_1",
            user_message="read and report",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            interrupt_timeout_s=0.05,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=decide),
        )
    assert decisions
    assert any(
        event["type"] == "user.message"
        for _, batch in fa.beta.sessions.events.sent_events
        for event in batch
    )
    assert not _confirmations(fa), "fresh spend denial must precede the real tool-confirmation POST"
