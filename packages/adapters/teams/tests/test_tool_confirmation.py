"""Teams' confirmation card: posted in the conversation, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams.tool_confirmation import (
    VERB,
    TeamsConfirmationCards,
    confirmation_adaptive_card,
)
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationPrompt,
    prompt_for_tool_call,
)
from daimon.core.posted_controls.confirmation import (
    EXPIRED_MESSAGE,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    build_confirmation_card,
)
from daimon.core.tool_safety import ToolCall
from microsoft_teams.api import AdaptiveCardInvokeActivity

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


@pytest.mark.parametrize(
    "value",
    [
        "@everyone @here <@123456789012345678> <@&123456789012345678> <#123456789012345678>",
        "<!channel> <!here> <@U123>",
        "[text](https://example.test/path) <https://example.test/path|text>",
        "*x* `x` ```x``` rest",
    ],
)
def test_tool_words_stay_literal_in_title_and_details(value: str) -> None:
    prompt = _prompt().model_copy(
        update={
            "title": f'Publish "{value}"?',
            "consequence": f"Sharing {value} is public.",
            "detail_lines": (f"File: {value}",),
        }
    )
    card = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    payload = card.model_dump(by_alias=True, exclude_none=True)
    items = payload["body"][0]["items"]
    assert items[0]["type"] == "RichTextBlock"
    assert items[0]["inlines"][0]["text"] == prompt.title
    assert items[1]["type"] == "RichTextBlock"
    assert items[1]["inlines"][0]["text"] == prompt.consequence
    details = next(item for item in items if item.get("id") == "approval-details")
    assert details["items"][0]["type"] == "RichTextBlock"
    assert details["items"][0]["inlines"][0]["text"] == f"File: {value}"


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

    result = await asyncio.wait_for(waiting, timeout=1)
    assert (result.answer if isinstance(result, ApprovedConfirmation) else result) == answer
    assert answered.value == headline, "the click is acknowledged briefly"
    # The answered card goes through the card's edit queue, not the invoke
    # response, so a later Stopped retire can never be overwritten by it.
    for _ in range(50):
        if len(sender.sent) >= 2:
            break
        await asyncio.sleep(0)
    body = _json(sender, len(sender.sent) - 1)
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


async def test_approved_card_can_retire_stopped_before_allow_is_sent() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting, token = await _post(cards, sender)
    await cards.on_action(_click(token, "approve", AAD_OBJECT_ID))

    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation)
    await result.retire_unsent()

    assert sender.activities[-1].id == "m-1"
    assert "Stopped" in _json(sender, -1)


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


def test_upload_card_has_expander_and_state_color() -> None:
    from daimon.adapters.teams.tool_confirmation import confirmation_adaptive_card
    from daimon.core.posted_controls.confirmation import build_confirmation_card

    prompt = _prompt().model_copy(
        update={
            "title": 'Upload "cg_meme.csv" to notebook "memecoin-scan"?',
            "consequence": "Anyone with the notebook's link can open these files.",
            "requester_display_name": "Ada Lovelace",
        }
    )
    pending = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    ).model_dump_json(by_alias=True)
    assert "Action.ToggleVisibility" in pending
    assert "CodeBlock" not in pending and "Tool:" not in pending
    assert '"title":"Approve"' in pending and '"title":"Deny"' in pending
    assert "Ada Lovelace" in pending and "warning" in pending
    assert "Only Ada Lovelace can approve or deny" in pending
    assert '"text":"Expires {{TIME(' in pending
    assert " · " not in pending
    approved = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="approved"), prompt
    ).model_dump_json(by_alias=True)
    assert "good" in approved and "Action.Execute" not in approved
    assert " · " not in approved
    denied = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="denied"), prompt
    ).model_dump_json(by_alias=True)
    assert "emphasis" in denied and "Action.Execute" not in denied
    assert " · " not in denied


async def test_a_stop_after_approve_lands_after_the_approved_card() -> None:
    """Re-review of #504: the Approved card returned as the invoke response
    could land after a Stopped retire that followed it."""
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting, token = await _post(cards, sender)
    await cards.on_action(_click(token, "approve", AAD_OBJECT_ID.upper()))
    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation)
    await result.retire_unsent()
    for _ in range(50):
        if len(sender.sent) >= 3:
            break
        await asyncio.sleep(0)
    approved, stopped = _json(sender, 1), _json(sender, 2)
    assert "Approved" in approved
    assert "Stopped" in stopped, "Stopped is the last state the card shows"
