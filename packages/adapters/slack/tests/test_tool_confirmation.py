"""Slack's confirmation card: posted in the thread, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.tool_confirmation import SlackConfirmationCards
from daimon.core.confirmation import ConfirmationPrompt, prompt_for_tool_call
from daimon.core.posted_controls.confirmation import NOT_YOURS_MESSAGE
from daimon.core.tool_safety import ToolCall
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
) -> tuple[asyncio.Task[Any], str]:
    hook = cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9")
    waiting = asyncio.create_task(hook(prompt or _prompt()))
    while not client.chat_postMessage.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C1"
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
        text=NOT_YOURS_MESSAGE.format(requester="<@U1>"),
        mrkdwn=False,
        parse="none",
    )
    assert not waiting.done(), "a stranger's click answers nothing"

    await cards.handle_click(_click(approve_id, "U1"))

    assert await asyncio.wait_for(waiting, timeout=1) == "approved"
    update = client.chat_update.await_args.kwargs
    assert update["ts"] == "1700.1"
    assert update["text"].startswith("Approved")
    assert not [b for b in update["attachments"][0]["blocks"] if b["type"] == "actions"]


async def test_the_requesters_deny_answers_denied() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    await cards.handle_click(_click(approve_id.replace(":approve", ":deny"), "U1"))

    assert await asyncio.wait_for(waiting, timeout=1) == "denied"


async def test_tool_words_are_literal_in_card_fallback_and_private_details() -> None:
    value = "*x* `x` @everyone <@123456789012345678>"
    prompt = _prompt().model_copy(
        update={"title": f'Publish "{value}"?', "detail_lines": (f"File: {value}",)}
    )
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client, prompt)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["mrkdwn"] is False and kwargs["parse"] == "none"
    assert kwargs["text"] == prompt.title
    assert kwargs["attachments"][0]["blocks"][0]["text"] == {
        "type": "plain_text",
        "text": prompt.title,
    }

    await cards.handle_click(_click(approve_id.replace(":approve", ":details"), "U2"))
    details = client.chat_postEphemeral.await_args.kwargs
    assert details["text"] == f"File: {value}"
    assert details["mrkdwn"] is False and details["parse"] == "none"
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting


async def test_an_unanswered_card_expires_and_is_retired() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    hook = cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9")

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
    from daimon.core.turn import run_turn
    from daimon.core.turn.approvals import interactive_decider
    from daimon.core.turn.posture import BillingExempt, PolicyApproval
    from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

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
        run_turn(
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
        cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9"), monkeypatch
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
    hook = cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9")
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
    assert await waiting == "approved"
    assert client.chat_update.await_args.kwargs["attachments"][0]["color"] == "#57F287"
