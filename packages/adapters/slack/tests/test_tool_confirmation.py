"""Slack's confirmation card: posted in the thread, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import tool_confirmation as confirmation_module
from daimon.adapters.slack.tool_confirmation import SlackConfirmationCards
from daimon.core.authz import Subject
from daimon.core.config import RecruitmentSettings, TurnSettings
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationPrompt,
    ToolRuleCardReference,
    prompt_for_tool_call,
)
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCardState,
    build_confirmation_blocks,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import drain_card_edits
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.privacy import TurnPrivacy
from slack_sdk.web.async_client import AsyncWebClient


def _prompt(*, expires_in: timedelta = timedelta(minutes=10)) -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="U1", now=datetime.now(UTC))
    return prompt.model_copy(update={"expires_at": datetime.now(UTC) + expires_in})


def _client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "1700.1"})
    client.chat_update = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    return client


def _click(action_id: str, user: str) -> dict[str, Any]:
    return {"actions": [{"action_id": action_id}], "user": {"id": user}}


async def _post(
    cards: SlackConfirmationCards,
    client: MagicMock,
    prompt: ConfirmationPrompt | None = None,
    *,
    channel: str = "C1",
) -> tuple[asyncio.Task[Any], str]:
    hook = cards.hook(
        cast(AsyncWebClient, client), team_id="T_HOST", channel=channel, thread_ts="1699.9"
    )
    waiting = asyncio.create_task(hook(prompt or _prompt()))
    while not client.chat_postMessage.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == channel
    assert kwargs["thread_ts"] == "1699.9"
    (actions,) = [b for b in kwargs["attachments"][0]["blocks"] if b["type"] == "actions"]
    approve_id = actions["elements"][0]["action_id"]
    return waiting, approve_id


async def test_the_requesters_approve_answers_approved_and_edits_the_card() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    await cards.handle_click(_click(approve_id, "U2"))
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U2",
        thread_ts="1699.9",
        text=NOT_YOURS_MESSAGE.format(requester="<@U1>"),
        mrkdwn=False,
        parse="none",
    )
    assert not waiting.done(), "a stranger's click answers nothing"

    await cards.handle_click(_click(approve_id, "U1"))

    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation) and result.answer == "approved"
    update = client.chat_update.await_args.kwargs
    assert update["ts"] == "1700.1"
    assert update["text"].startswith("Approved")
    assert not [b for b in update["attachments"][0]["blocks"] if b["type"] == "actions"]

    for channel, posted_ts in (("C1", "1699.9"), ("D1", "1700.1")):
        other_cards = SlackConfirmationCards()
        other_client = _client()
        other_client.chat_postMessage.return_value = {"ts": posted_ts}
        other_waiting, other_approve_id = await _post(other_cards, other_client, channel=channel)
        await other_cards.handle_click(_click(other_approve_id, "U2"))
        assert other_client.chat_postEphemeral.await_args.kwargs.get("thread_ts") is None
        other_waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await other_waiting


async def test_the_requesters_deny_answers_denied() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    await cards.handle_click(_click(approve_id.replace(":approve", ":deny"), "U1"))

    assert await asyncio.wait_for(waiting, timeout=1) == "denied"


@pytest.mark.parametrize(
    ("enabled", "state", "footer"),
    [
        (True, "approved", "Approved once by <@U1>"),
        (True, "denied", "Denied by <@U1>"),
        (False, "approved", "by <@U1>"),
        (False, "denied", "by <@U1>"),
    ],
)
def test_requester_answer_footer(enabled: bool, state: ConfirmationCardState, footer: str) -> None:
    prompt = _prompt().model_copy(update={"tool_rules_enabled": enabled})
    card = build_confirmation_card(
        prompt,
        state=state,
        answered_by_platform_user_id="U1",
        how="once" if enabled else None,
    )
    assert build_confirmation_blocks(card, prompt=prompt)[-1]["elements"][0]["text"] == footer


async def test_always_click_checks_admin_then_claims_and_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = SlackConfirmationCards()
    client = _client()
    token, future = cards._controls.pending.open()
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
                channel_id="C1",
            ),
        }
    )
    subject = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="U2"))
    posted = confirmation_module._PostedCard(
        prompt,
        client,
        "C1",
        "1700.1",
        "1699.9",
        rule_factory=MagicMock(),
        rule_subject=subject,
    )
    cards._controls.cards[token] = posted
    save = AsyncMock(return_value="saved")
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    action = f"dcf:{token}:always"
    await cards.handle_click(_click(action, "U2"))
    assert future.result() == "approved"
    save.assert_awaited_once()
    assert save.await_args.kwargs["scope"] == "agent"
    blocks = client.chat_update.await_args.kwargs["attachments"][0]["blocks"]
    assert blocks[-1]["elements"][0]["text"] == "Always allowed by <@U2>"
    await cards.handle_click(_click(action, "U2"))
    assert save.await_count == 1


async def test_always_click_refuses_nonadmins_without_settling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = SlackConfirmationCards()
    client = _client()
    token, future = cards._controls.pending.open()
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
                channel_id="C1",
            ),
        }
    )
    cards._controls.cards[token] = confirmation_module._PostedCard(
        prompt,
        client,
        "C1",
        "1700.1",
        "1699.9",
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject()),
    )
    save = AsyncMock()
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    for user, expected in (
        (
            "U1",
            "Only an admin can allow this for everyone.\nAsk an admin, or use Approve for just this time.",
        ),
        ("U2", "Only an admin can allow this for everyone."),
    ):
        await cards.handle_click(_click(f"dcf:{token}:always", user))
        assert client.chat_postEphemeral.await_args.kwargs["text"] == expected
    assert not future.done()
    save.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("save failed"), asyncio.CancelledError()])
async def test_always_save_failure_marks_edit_done_and_approves_once(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    cards = SlackConfirmationCards()
    client = _client()
    token, future = cards._controls.pending.open()
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    posted = confirmation_module._PostedCard(
        prompt,
        client,
        "C1",
        "1700.1",
        "1699.9",
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject(is_admin=True, platform_user_id="U2")),
    )
    cards._controls.cards[token] = posted
    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(side_effect=failure))
    click = cards.handle_click(_click(f"dcf:{token}:always", "U2"))
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await click
        for _ in range(50):
            if client.chat_update.await_count:
                break
            await asyncio.sleep(0)
    else:
        await click
    assert await drain_card_edits(1) == 0
    assert future.result() == "approved"
    assert posted.answered_edit_done.is_set()
    blocks = client.chat_update.await_args.kwargs["attachments"][0]["blocks"]
    assert blocks[-1]["elements"][0]["text"] == "Approved once by <@U2>"


async def test_always_role_check_loses_to_deny_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = SlackConfirmationCards()
    client = _client()
    token, future = cards._controls.pending.open()
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def role(_clicker: str) -> Subject:
        started.set()
        await release.wait()
        return Subject(is_admin=True, platform_user_id="U2")

    cards._controls.cards[token] = confirmation_module._PostedCard(
        prompt,
        client,
        "C1",
        "1700.1",
        "1699.9",
        rule_factory=MagicMock(),
        rule_subject=role,
    )
    save = AsyncMock()
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    always = asyncio.create_task(cards.handle_click(_click(f"dcf:{token}:always", "U2")))
    await started.wait()
    await cards.handle_click(_click(f"dcf:{token}:deny", "U1"))
    assert future.result() == "denied"
    release.set()
    await always
    save.assert_not_awaited()
    assert client.chat_postEphemeral.await_args.kwargs["text"] == (
        "This request was already answered."
    )


async def test_stale_always_click_approves_once_and_notifies_clicker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = SlackConfirmationCards()
    client = _client()
    token, future = cards._controls.pending.open()
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    cards._controls.cards[token] = confirmation_module._PostedCard(
        prompt,
        client,
        "C1",
        "1700.1",
        "1699.9",
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject(is_admin=True, platform_user_id="U2")),
    )
    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(return_value="stale"))
    await cards.handle_click(_click(f"dcf:{token}:always", "U2"))
    assert await drain_card_edits(1) == 0
    assert future.result() == "approved"
    blocks = client.chat_update.await_args.kwargs["attachments"][0]["blocks"]
    assert blocks[-1]["elements"][0]["text"] == "Approved once by <@U2>"
    assert client.chat_postEphemeral.await_args.kwargs["text"] == (
        "Approved this time only.\n"
        "Someone changed this tool's setting while the card waited, so Always allow wasn't saved."
    )


@pytest.mark.parametrize(
    ("value", "safe"),
    [
        (
            "<!channel> <!here> <@U123> & <@U456>",
            "&lt;!channel&gt; &lt;!here&gt; &lt;@U123&gt; &amp; &lt;@U456&gt;",
        ),
        ("<https://example.test/path|text>", "&lt;https://example.test/path|text&gt;"),
        ("*x* `x` ```x``` rest", "*x* `x` ```x``` rest"),
    ],
)
async def test_tool_words_are_literal_in_card_fallback_and_private_details(
    value: str, safe: str
) -> None:
    prompt = _prompt().model_copy(
        update={
            "title": f'Publish "{value}"?',
            "consequence": f"Sharing {value} is public.",
            "detail_lines": (f"File: {value}",),
        }
    )
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client, prompt)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["mrkdwn"] is False and kwargs["parse"] == "none"
    assert kwargs["text"] == f'Publish "{safe}"?\nSharing {safe} is public.'
    assert kwargs["attachments"][0]["blocks"][0]["text"] == {
        "type": "plain_text",
        "text": prompt.title,
    }
    assert kwargs["attachments"][0]["blocks"][1]["text"] == {
        "type": "plain_text",
        "text": prompt.consequence,
    }

    await cards.handle_click(_click(approve_id.replace(":approve", ":details"), "U2"))
    details = client.chat_postEphemeral.await_args.kwargs
    assert details["thread_ts"] == "1699.9"
    assert details["text"] == f"File: {safe}"
    assert details["mrkdwn"] is False and details["parse"] == "none"
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting


async def test_approved_card_can_retire_stopped_before_allow_is_sent() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)
    await cards.handle_click(_click(approve_id, "U1"))

    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation)
    await result.retire_unsent()

    assert client.chat_update.await_count == 2
    assert client.chat_update.await_args.kwargs["text"].startswith("Stopped")


async def test_an_unanswered_card_expires_and_is_retired() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    hook = cards.hook(
        cast(AsyncWebClient, client), team_id="T_HOST", channel="C1", thread_ts="1699.9"
    )

    answer = await hook(_prompt(expires_in=timedelta(milliseconds=10)))

    assert answer == "expired"
    assert client.chat_update.await_args.kwargs["text"].startswith("Expired")


async def test_a_cancelled_wait_retires_the_card_and_ignores_late_clicks() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    assert client.chat_update.await_args.kwargs["text"].startswith("Stopped")
    await cards.handle_click(_click(approve_id, "U1"))
    assert client.chat_update.await_count == 1, "a late click changes nothing"


async def _run_turn_with_stalled_card_retire(
    hook: object, monkeypatch: pytest.MonkeyPatch
) -> tuple[float, object]:
    """Drive a real turn whose write card is up when a 100ms deadline hits,
    with the platform stalled on the card edit that retires it."""
    import daimon.core.turn.driver as driver_mod
    from anthropic.types.beta.sessions import (
        BetaManagedAgentsAgentMCPToolUseEvent,
        BetaManagedAgentsSessionEndTurn,
        BetaManagedAgentsSessionRequiresAction,
        BetaManagedAgentsSessionStatusIdleEvent,
    )
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.core.turn.approvals import interactive_decider
    from daimon.core.turn.posture import BillingExempt, PolicyApproval
    from daimon.testing.turn_fakes import (
        FakeAnthropic,
        RecordingLifecycle,
        YieldEvent,
        run_turn_on_leg,
    )

    monkeypatch.setattr(driver_mod, "CLEANUP_BUDGET_S", 0.2)
    at = datetime.now(UTC)
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                BetaManagedAgentsAgentMCPToolUseEvent(
                    id="tu_w",
                    type="agent.mcp_tool_use",
                    name="create_issue",
                    input={"title": "x"},
                    mcp_server_name="linear",
                    processed_at=at,
                )
            ),
            YieldEvent(
                BetaManagedAgentsSessionStatusIdleEvent(
                    id="pause",
                    type="session.status_idle",
                    stop_reason=BetaManagedAgentsSessionRequiresAction(
                        type="requires_action", event_ids=["tu_w"]
                    ),
                    processed_at=at,
                )
            ),
            YieldEvent(
                BetaManagedAgentsSessionStatusIdleEvent(
                    id="end",
                    type="session.status_idle",
                    stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
                    processed_at=at,
                )
            ),
        ]
    ]
    started = asyncio.get_running_loop().time()
    final = await asyncio.wait_for(
        run_turn_on_leg(
            privacy=TurnPrivacy("slack", RecruitmentSettings()),
            anthropic=fa,  # type: ignore[arg-type]
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="cli-operator-run"),
            tool_confirmation=PolicyApproval(
                decide=interactive_decider(
                    ToolSafetyPolicy(enabled=True),
                    requester_platform_user_id="U1",
                    confirm=hook,  # type: ignore[arg-type]
                )
            ),
            deadline=datetime.now(UTC) + timedelta(milliseconds=100),
        ),
        timeout=5,
    )
    elapsed = asyncio.get_running_loop().time() - started
    sent = [
        e
        for _sid, batch in fa.beta.sessions.events.sent_events
        for e in batch
        if e["type"] == "user.tool_confirmation"
    ]
    assert all(e["result"] != "allow" for e in sent), "a stalled cleanup never lets the write run"
    return elapsed, final


async def test_a_stalled_slack_retire_cannot_hold_the_turn_past_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.adapters.slack.tool_confirmation as hook_mod

    monkeypatch.setattr(hook_mod, "EDIT_TIMEOUT_S", 60.0)  # the driver's budget must bound it

    async def stalled_update(**_kwargs: object) -> None:
        await asyncio.Event().wait()  # Slack never answers

    client = _client()
    client.chat_update = stalled_update
    cards = SlackConfirmationCards()

    elapsed, final = await _run_turn_with_stalled_card_retire(
        cards.hook(
            cast(AsyncWebClient, client), team_id="T_HOST", channel="C1", thread_ts="1699.9"
        ),
        monkeypatch,
    )

    assert final.error is not None and final.error.kind == "ceiling"  # type: ignore[attr-defined]
    assert elapsed < 1.5, f"took {elapsed:.2f}s"
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "turn.decide_blocked"]


async def test_upload_card_has_color_and_private_details() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    prompt = _prompt().model_copy(
        update={
            "title": 'Upload "cg_meme.csv" to notebook "memecoin-scan"?',
            "consequence": "Anyone with the notebook's link can open these files.",
        }
    )
    hook = cards.hook(
        cast(AsyncWebClient, client), team_id="T_HOST", channel="C1", thread_ts="1699.9"
    )
    waiting = asyncio.create_task(hook(prompt))
    while not client.chat_postMessage.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    attachment = client.chat_postMessage.await_args.kwargs["attachments"][0]
    assert attachment["color"] == "#FEE75C"
    actions = next(block for block in attachment["blocks"] if block["type"] == "actions")
    assert [button["text"]["text"] for button in actions["elements"]] == [
        "Approve",
        "Deny",
        "Details",
    ]
    await cards.handle_click(_click(actions["elements"][2]["action_id"], "U2"))
    assert client.chat_postEphemeral.await_args.kwargs["text"] == "\n".join(prompt.detail_lines)
    assert not waiting.done()
    await cards.handle_click(_click(actions["elements"][0]["action_id"], "U1"))
    assert isinstance(await waiting, ApprovedConfirmation)
    assert client.chat_update.await_args.kwargs["attachments"][0]["color"] == "#57F287"


async def test_admin_confirmation_rechecks_each_click_before_settlement() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    prompt = _prompt().model_copy(update={"admin": True})
    prepare = AsyncMock(
        return_value=prompt.model_copy(update={"detail_lines": ("List price: verified",)})
    )
    check = AsyncMock(return_value="Current admin permission required.")
    waiting = asyncio.create_task(
        cards.hook(
            cast(AsyncWebClient, client),
            team_id="T_HOST",
            channel="C1",
            thread_ts="1699.9",
            prepare_admin=prepare,
            check_admin=check,
        )(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not client.chat_postMessage.await_count:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        blocks = client.chat_postMessage.await_args.kwargs["attachments"][0]["blocks"]
        assert any(b.get("text", {}).get("text") == "List price: verified" for b in blocks)
        actions = next(b for b in blocks if b["type"] == "actions")
        assert actions["elements"][0]["text"]["text"] == "Confirm"
        action_id = actions["elements"][0]["action_id"]
        await cards.handle_click(_click(action_id, "U1"))
        assert (
            client.chat_postEphemeral.await_args.kwargs["text"]
            == "Current admin permission required."
        )
        client.chat_update.assert_not_awaited()
        assert not waiting.done()
        check.return_value = None
        await cards.handle_click(_click(action_id, "U1"))
        assert isinstance(await asyncio.wait_for(waiting, timeout=1), ApprovedConfirmation)
        assert check.await_count == 2
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_admin_confirmation_without_a_verifier_posts_nothing() -> None:
    client = _client()
    cards = SlackConfirmationCards()
    prompt = _prompt().model_copy(update={"admin": True})
    assert (
        await cards.hook(
            cast(AsyncWebClient, client), team_id="T_HOST", channel="C1", thread_ts="1699.9"
        )(prompt)
        == "denied"
    )
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize("case", ["expired", "exception", "wrong_requester", "legacy"])
async def test_admin_await_cannot_bypass_requester_expiry_or_fail_closed(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.posted_controls import lifecycle

    cards, client = SlackConfirmationCards(), _client()
    prompt = _prompt().model_copy(update={"admin": case != "legacy"})

    async def verify(*args: object) -> None:
        await asyncio.sleep(0)
        if case == "exception":
            raise RuntimeError("role lookup unavailable")
        if case == "expired":
            clock = MagicMock()
            clock.now.return_value = prompt.expires_at
            monkeypatch.setattr(lifecycle, "datetime", clock)

    check = AsyncMock(side_effect=verify)
    waiting = asyncio.create_task(
        cards.hook(
            cast(AsyncWebClient, client),
            team_id="T_HOST",
            channel="C1",
            thread_ts="1699.9",
            prepare_admin=AsyncMock(return_value=prompt),
            check_admin=check,
        )(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not client.chat_postMessage.await_count:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        blocks = client.chat_postMessage.await_args.kwargs["attachments"][0]["blocks"]
        actions = next(b for b in blocks if b["type"] == "actions")
        await cards.handle_click(
            _click(actions["elements"][0]["action_id"], "U2" if case == "wrong_requester" else "U1")
        )
        if case == "legacy":
            assert isinstance(await asyncio.wait_for(waiting, 1), ApprovedConfirmation)
            check.assert_not_awaited()
        else:
            client.chat_update.assert_not_awaited()
            client.chat_postEphemeral.assert_awaited_once()
            assert not waiting.done()
            if case == "wrong_requester":
                check.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_runtime_hook_reuses_live_workspace_admin_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from daimon.core import confirmation

    tenant, account = uuid4(), uuid4()
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    client = _client()
    client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"is_admin": True, "team_id": "T_HOST"}}
    )
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    SlackConfirmationCards().hook(
        cast(AsyncWebClient, client),
        team_id="T_HOST",
        channel="C1",
        thread_ts="1699.9",
        runtime=runtime,
        tenant_id=tenant,
        account_id=account,
    )
    kwargs = factory.call_args.kwargs
    assert kwargs["tenant_id"] == tenant and kwargs["account_id"] == account
    check = kwargs["current_admin"]
    assert await check("U1") is True
    client.users_info.return_value = {
        "ok": True,
        "user": {"is_admin": False, "team_id": "T_HOST"},
    }
    assert await check("U1") is False
    assert client.users_info.await_count == 2


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired", "stopped"])
def test_complete_backend_loss_list_survives_native_card_rendering(state):
    from daimon.adapters.slack.tool_confirmation import _attachment
    from daimon.core.posted_controls.confirmation import build_confirmation_card

    losses = "Won't be available on openai: files, mcp, memory, model_controls, repository, sandbox, skills, tools"
    prompt = _prompt().model_copy(
        update={
            "admin": True,
            "action": "Switch Research. " + losses,
            "denied_action": "Model provider unchanged",
            "consequence": losses,
            "detail_lines": ("Optional skill: " + "x" * 1000,),
        }
    )
    card = build_confirmation_card(
        prompt, state=state, token="abcdefgh" if state == "pending" else None
    )
    rendered = _attachment(card, prompt)[0]["blocks"]
    texts = [block["text"]["text"] for block in rendered if block["type"] == "section"]
    assert any(losses in text for text in texts) == (state in {"pending", "approved"})


async def test_blocked_always_save_does_not_block_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client, prompt)
    token = approve_id.split(":")[1]
    posted = cards._controls.cards[token]
    object.__setattr__(posted, "rule_factory", MagicMock())
    object.__setattr__(
        posted,
        "rule_subject",
        AsyncMock(return_value=Subject(is_admin=True, platform_user_id="U2")),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        entered.set()
        await release.wait()
        return "saved"

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    click = asyncio.create_task(cards.handle_click(_click(f"dcf:{token}:always", "U2")))
    await entered.wait()
    result = await asyncio.wait_for(waiting, 1)
    assert isinstance(result, ApprovedConfirmation)
    await asyncio.wait_for(result.retire_unsent(), 0.5)
    assert not click.done()
    release.set()
    await click
    assert await drain_card_edits(1) == 0
    assert client.chat_update.await_args.kwargs["text"].startswith("Stopped")


async def test_turn_cancel_after_always_settle_keeps_stopped_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client, prompt)
    token = approve_id.split(":")[1]
    posted = cards._controls.cards[token]
    object.__setattr__(posted, "rule_factory", MagicMock())
    object.__setattr__(
        posted,
        "rule_subject",
        AsyncMock(return_value=Subject(is_admin=True, platform_user_id="U2")),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        waiting.cancel()
        entered.set()
        await release.wait()
        return "saved"

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    click = asyncio.create_task(cards.handle_click(_click(f"dcf:{token}:always", "U2")))
    await entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert posted.retired.is_set()
    release.set()
    await click
    assert await drain_card_edits(1) == 0
    assert client.chat_update.await_args.kwargs["text"].startswith("Stopped")


async def test_cancelled_retirement_wait_can_be_retried() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    editing = asyncio.Event()
    release = asyncio.Event()

    async def slow_update(**_kwargs: object) -> None:
        editing.set()
        await release.wait()

    client.chat_update = AsyncMock(side_effect=slow_update)
    waiting, approve_id = await _post(cards, client)
    click = asyncio.create_task(cards.handle_click(_click(approve_id, "U1")))
    result = await asyncio.wait_for(waiting, 1)
    assert isinstance(result, ApprovedConfirmation)
    await editing.wait()
    retire = asyncio.create_task(result.retire_unsent())
    await asyncio.sleep(0)
    assert not retire.done()
    retire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retire
    release.set()
    await click
    await asyncio.wait_for(result.retire_unsent(), 1)
    assert client.chat_update.await_args.kwargs["text"].startswith("Stopped")


async def test_runtime_hook_binds_pin_cards_to_the_admitted_origin_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from daimon.core import confirmation
    from mux.contracts.ids import ChannelRef

    tenant, account = uuid4(), uuid4()
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)

    def hook(admission):
        SlackConfirmationCards().hook(
            cast(AsyncWebClient, _client()),
            team_id="T_HOST",
            channel="C1",
            thread_ts="1699.9",
            runtime=runtime,
            admission=admission,
            tenant_id=tenant,
            account_id=account,
        )
        return factory.call_args.kwargs["pin_channel"]

    assert hook(SimpleNamespace(origin_channel_id="C_ORIGIN")) == ChannelRef(
        tenant_id=str(tenant), platform="slack", channel_id="C_ORIGIN"
    )
    assert hook(None) is None, "no admitted channel, no pin card"


async def test_a_card_still_open_posts_one_reminder_in_its_thread(monkeypatch) -> None:
    """ROOT 2: a long card pings its requester once, in the card's thread."""
    from daimon.core.posted_controls import lifecycle

    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.02)
    monkeypatch.setattr(lifecycle, "REMIND_MIN_LEFT_S", 0.05)
    cards = SlackConfirmationCards()
    client = _client()
    waiting, _approve = await _post(cards, client, _prompt(expires_in=timedelta(seconds=0.3)))
    assert await waiting == "expired"
    posts = [call.kwargs for call in client.chat_postMessage.await_args_list]
    reminders = [kwargs for kwargs in posts if "still waiting" in str(kwargs.get("text"))]
    assert reminders == [
        {
            "channel": "C1",
            "thread_ts": "1699.9",
            "text": "<@U1> This is still waiting for your click.",
        }
    ]
