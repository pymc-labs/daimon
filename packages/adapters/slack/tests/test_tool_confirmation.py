"""Slack's confirmation card: posted in the thread, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

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


async def _post(cards: SlackConfirmationCards, client: MagicMock) -> tuple[asyncio.Task[Any], str]:
    hook = cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9")
    waiting = asyncio.create_task(hook(_prompt()))
    while not client.chat_postMessage.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["thread_ts"] == "1699.9"
    (actions,) = [b for b in kwargs["blocks"] if b["type"] == "actions"]
    approve_id = actions["elements"][0]["action_id"]
    return waiting, approve_id


async def test_the_requesters_approve_answers_approved_and_edits_the_card() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    await cards.handle_click(_click(approve_id, "U2"))
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1", user="U2", text=NOT_YOURS_MESSAGE
    )
    assert not waiting.done(), "a stranger's click answers nothing"

    await cards.handle_click(_click(approve_id, "U1"))

    assert await asyncio.wait_for(waiting, timeout=1) == "approved"
    update = client.chat_update.await_args.kwargs
    assert update["ts"] == "1700.1"
    assert update["text"].startswith("✅ Approved")
    assert not [b for b in update["blocks"] if b["type"] == "actions"]


async def test_the_requesters_deny_answers_denied() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    await cards.handle_click(_click(approve_id.replace(":approve", ":deny"), "U1"))

    assert await asyncio.wait_for(waiting, timeout=1) == "denied"


async def test_an_unanswered_card_expires_and_is_retired() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    hook = cards.hook(cast(AsyncWebClient, client), channel="C1", thread_ts="1699.9")

    answer = await hook(_prompt(expires_in=timedelta(milliseconds=10)))

    assert answer == "expired"
    assert client.chat_update.await_args.kwargs["text"].startswith("⌛")


async def test_a_cancelled_wait_retires_the_card_and_ignores_late_clicks() -> None:
    cards = SlackConfirmationCards()
    client = _client()
    waiting, approve_id = await _post(cards, client)

    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    assert client.chat_update.await_args.kwargs["text"].startswith("🛡️ Denied")
    await cards.handle_click(_click(approve_id, "U1"))
    assert client.chat_update.await_count == 1, "a late click changes nothing"
