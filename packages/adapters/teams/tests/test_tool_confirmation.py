"""Teams' confirmation card: posted in the conversation, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams.tool_confirmation import VERB, TeamsConfirmationCards
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt, prompt_for_tool_call
from daimon.core.posted_controls.confirmation import (
    EXPIRED_MESSAGE,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
)
from daimon.core.tool_safety import ToolCall
from microsoft_teams.api import AdaptiveCardActionCardResponse, AdaptiveCardInvokeActivity

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    USER_NAME,
    FakeSender,
    make_card_action,
)


def _prompt(*, expires_in: timedelta = timedelta(minutes=10)) -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
    )
    now = datetime.now(UTC)
    prompt = prompt_for_tool_call(call, requester_platform_user_id=AAD_OBJECT_ID, now=now)
    return prompt.model_copy(update={"expires_at": now + expires_in})


def _click(token: str, op: str, user: str) -> Any:
    payload = make_card_action(VERB, op, user=user, token=token)
    return SimpleNamespace(activity=AdaptiveCardInvokeActivity.model_validate(payload))


def _json(sender: FakeSender, index: int) -> str:
    return sender.activities[index].model_dump_json(by_alias=True)


async def _post(
    cards: TeamsConfirmationCards, sender: FakeSender
) -> tuple[asyncio.Task[ConfirmationAnswer], str]:
    hook = cards.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL)
    waiting = asyncio.create_task(hook(_prompt()))
    while not sender.sent:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sender.sent[0][0] == CONVERSATION_ID and sender.sent[0][2] == SERVICE_URL
    token = re.search(r'"token":"([^"]+)"', _json(sender, 0))
    assert token is not None, "the pending card's buttons carry the token"
    return waiting, token.group(1)


@pytest.mark.parametrize(
    ("op", "answer", "headline"),
    [("approve", "approved", "Approved"), ("deny", "denied", "Denied")],
)
async def test_only_the_requesters_click_answers_and_replaces_the_card(
    op: str, answer: ConfirmationAnswer, headline: str
) -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting, token = await _post(cards, sender)

    refused = await cards.on_action(_click(token, op, OTHER_AAD_OBJECT_ID))
    assert refused.value == NOT_YOURS_MESSAGE.format(requester="requester")
    assert not waiting.done(), "a stranger's click answers nothing"

    answered = await cards.on_action(_click(token, op, AAD_OBJECT_ID.upper()))

    assert await asyncio.wait_for(waiting, timeout=1) == answer
    assert isinstance(answered, AdaptiveCardActionCardResponse)
    body = answered.value.model_dump_json(by_alias=True)
    assert headline in body, "the card shows the answer"
    assert f"by {USER_NAME}" in body, "an answer names who"
    assert "Action.Execute" not in body, "an answered card has no live buttons"


async def test_an_unanswered_card_expires_and_is_retired() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    hook = cards.hook(conversation_id=CONVERSATION_ID, service_url=None)

    answer = await hook(_prompt(expires_in=timedelta(milliseconds=10)))

    assert answer == "expired"
    assert sender.activities[-1].id == "m-1", "the posted card is edited in place"
    assert "Expired" in _json(sender, -1) and "Action.Execute" not in _json(sender, -1)
    token = re.search(r'"token":"([^"]+)"', _json(sender, 0))
    assert token is not None
    assert cards._controls.missing_message(token.group(1)) == EXPIRED_MESSAGE


async def test_a_cancelled_wait_retires_the_card_and_ignores_late_clicks() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting, token = await _post(cards, sender)

    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    assert sender.activities[-1].id == "m-1" and "Stopped" in _json(sender, -1)
    late = await cards.on_action(_click(token, "approve", AAD_OBJECT_ID))
    assert late.value == NO_LONGER_PENDING_MESSAGE
    assert len(sender.sent) == 2, "a late click changes nothing"


def test_grouped_card_has_expander_and_state_color() -> None:
    from daimon.adapters.teams.tool_confirmation import confirmation_adaptive_card
    from daimon.core.posted_controls.confirmation import build_confirmation_card

    prompt = _prompt().model_copy(
        update={
            "title": 'Upload 3 files to notebook "memecoin-scan"?',
            "items": ("cg_meme.csv", "launch_features.csv", "launch_curves.csv"),
            "consequence": "Anyone with the notebook's link can open these files.",
            "requester_display_name": "Ada Lovelace",
        }
    )
    pending = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    ).model_dump_json(by_alias=True)
    assert "Action.ToggleVisibility" in pending
    assert "CodeBlock" not in pending and "Tool:" not in pending
    assert "Approve all 3" in pending and "Deny all 3" in pending
    assert "Ada Lovelace" in pending and "warning" in pending
    approved = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="approved"), prompt
    ).model_dump_json(by_alias=True)
    assert "good" in approved and "Action.Execute" not in approved
    denied = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="denied"), prompt
    ).model_dump_json(by_alias=True)
    assert "emphasis" in denied and "Action.Execute" not in denied
