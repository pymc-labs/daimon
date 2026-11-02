"""Teams' confirmation card: posted in the conversation, answered by the requester's click."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.teams import tool_confirmation as confirmation_module
from daimon.adapters.teams.tool_confirmation import (
    VERB,
    TeamsConfirmationCards,
    confirmation_adaptive_card,
)
from daimon.core.authz import Subject
from daimon.core.config import TurnSettings
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationPrompt,
    ToolRuleCardReference,
    prompt_for_tool_call,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.native_admins import GroupMembersCache
from daimon.core.posted_controls.confirmation import (
    EXPIRED_MESSAGE,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCardState,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import drain_card_edits
from daimon.core.tool_safety import ToolCall
from microsoft_teams.api import (
    Account,
    AdaptiveCardInvokeActivity,
    ApiClient,
    ConversationAccount,
    ConversationReference,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.common import Client, ClientOptions
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    USER_NAME,
    FakeSender,
    TeamsApiFake,
    make_card_action,
    shown_text,
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
    return SimpleNamespace(
        activity=AdaptiveCardInvokeActivity.model_validate(payload), send=AsyncMock()
    )


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


def test_details_use_a_bounded_number_of_blocks_and_keep_every_line() -> None:
    lines = tuple(f"line {n}" for n in range(500))
    prompt = _prompt().model_copy(update={"detail_lines": lines})
    card = confirmation_adaptive_card(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    items = card.model_dump(by_alias=True, exclude_none=True)["body"][0]["items"]
    details = next(item for item in items if item.get("id") == "approval-details")
    texts = [block["inlines"][0]["text"] for block in details["items"]]
    assert len(texts) <= confirmation_module.MAX_DETAIL_BLOCKS
    assert "\n".join(texts).split("\n") == list(lines), "every line, in order"


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


@pytest.mark.parametrize(
    ("enabled", "state", "footer"),
    [
        (True, "approved", "Approved once by " + USER_NAME),
        (True, "denied", "Denied by " + USER_NAME),
        (False, "approved", "by " + USER_NAME),
        (False, "denied", "by " + USER_NAME),
    ],
)
def test_requester_answer_footer(enabled: bool, state: ConfirmationCardState, footer: str) -> None:
    prompt = _prompt().model_copy(update={"tool_rules_enabled": enabled})
    card = build_confirmation_card(
        prompt,
        state=state,
        answered_by_platform_user_id=AAD_OBJECT_ID,
        how="once" if enabled else None,
    )
    payload = confirmation_adaptive_card(card, prompt, answered_by=USER_NAME).model_dump(
        by_alias=True, exclude_none=True
    )
    assert payload["body"][0]["items"][-1]["text"] == footer


async def test_always_ack_precedes_live_admin_check() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
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
    checked = asyncio.Event()
    release = asyncio.Event()

    async def subject(_clicker: str) -> Subject:
        checked.set()
        await release.wait()
        return Subject()

    cards._controls.cards[token] = confirmation_module._PostedCard(
        prompt,
        CONVERSATION_ID,
        SERVICE_URL,
        "m-1",
        rule_factory=MagicMock(),
        rule_subject=subject,
    )
    click = _click(token, "always", OTHER_AAD_OBJECT_ID)
    response = await cards.on_action(click)
    assert response.value == "Checking you're an admin..."
    assert not checked.is_set() and not future.done()
    await checked.wait()
    release.set()
    await asyncio.gather(*cards._always_tasks)


async def test_always_action_refuses_nonadmin_privately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
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
                channel_id="home",
            ),
        }
    )
    cards._controls.cards[token] = confirmation_module._PostedCard(
        prompt,
        CONVERSATION_ID,
        SERVICE_URL,
        "m-1",
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject()),
    )
    save = AsyncMock()
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    requester_click = _click(token, "always", AAD_OBJECT_ID)
    other_click = _click(token, "always", OTHER_AAD_OBJECT_ID)
    requester = await cards.on_action(requester_click)
    other = await cards.on_action(other_click)
    assert requester.value == other.value == "Checking you're an admin..."
    await asyncio.gather(*cards._always_tasks)
    assert shown_text(requester_click.send.await_args.args[0]) == (
        "Only an admin can allow this for everyone.\n"
        "Ask an admin, or use Approve for just this time."
    )
    assert (
        shown_text(other_click.send.await_args.args[0])
        == "Only an admin can allow this for everyone."
    )
    assert other_click.send.await_args.args[0].attachments
    assert requester_click.send.await_args.args[0].recipient is None
    assert not future.done() and not sender.sent
    save.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("save failed"), asyncio.CancelledError()])
async def test_always_save_failure_marks_edit_done_and_approves_once(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
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
        CONVERSATION_ID,
        SERVICE_URL,
        "m-1",
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(
            return_value=Subject(is_admin=True, platform_user_id=OTHER_AAD_OBJECT_ID)
        ),
    )
    cards._controls.cards[token] = posted
    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(side_effect=failure))
    click = _click(token, "always", OTHER_AAD_OBJECT_ID)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await cards._on_always(click, token)
    else:
        await cards._on_always(click, token)
    assert future.result() == "approved"
    assert posted.answered_edit_done.is_set()
    for _ in range(50):
        if sender.sent:
            break
        await asyncio.sleep(0)
    assert "Approved once by" in _json(sender, -1)


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
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting = asyncio.create_task(
        cards.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL)(prompt)
    )
    while not sender.sent:
        await asyncio.sleep(0)
    token_match = re.search(r'"token":"([^"]+)"', _json(sender, 0))
    assert token_match is not None
    token = token_match.group(1)
    posted = cards._controls.cards[token]
    object.__setattr__(posted, "rule_factory", MagicMock())
    object.__setattr__(
        posted,
        "rule_subject",
        AsyncMock(return_value=Subject(is_admin=True, platform_user_id=OTHER_AAD_OBJECT_ID)),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        entered.set()
        await release.wait()
        return "saved"

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    await cards.on_action(_click(token, "always", OTHER_AAD_OBJECT_ID))
    await entered.wait()
    result = await asyncio.wait_for(waiting, 1)
    assert isinstance(result, ApprovedConfirmation)
    await asyncio.wait_for(result.retire_unsent(), 0.5)
    assert cards._always_tasks
    release.set()
    await asyncio.gather(*cards._always_tasks)
    assert await drain_card_edits(1) == 0
    assert "Stopped" in _json(sender, -1)


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
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting = asyncio.create_task(
        cards.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL)(prompt)
    )
    while not sender.sent:
        await asyncio.sleep(0)
    token_match = re.search(r'"token":"([^"]+)"', _json(sender, 0))
    assert token_match is not None
    token = token_match.group(1)
    posted = cards._controls.cards[token]
    object.__setattr__(posted, "rule_factory", MagicMock())
    object.__setattr__(
        posted,
        "rule_subject",
        AsyncMock(return_value=Subject(is_admin=True, platform_user_id=OTHER_AAD_OBJECT_ID)),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        waiting.cancel()
        entered.set()
        await release.wait()
        return "saved"

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    await cards.on_action(_click(token, "always", OTHER_AAD_OBJECT_ID))
    await entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert posted.retired.is_set()
    release.set()
    await asyncio.gather(*cards._always_tasks)
    assert await drain_card_edits(1) == 0
    assert "Stopped" in _json(sender, -1)


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


async def test_a_transient_send_failure_on_the_answered_card_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final review of #504: the token is claimed once the click is answered,
    so a failed card edit could never be repaired; it is retried instead."""
    from daimon.adapters.teams import tool_confirmation

    monkeypatch.setattr(tool_confirmation, "_CARD_SEND_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    waiting, token = await _post(cards, sender)
    real_send = sender.send
    failures = [ValueError("transient")]

    async def flaky_send(*args: object, **kwargs: object) -> object:
        if failures:
            raise failures.pop()
        return await real_send(*args, **kwargs)

    monkeypatch.setattr(sender, "send", flaky_send)
    await cards.on_action(_click(token, "deny", AAD_OBJECT_ID.upper()))
    assert await asyncio.wait_for(waiting, timeout=1) == "denied"
    for _ in range(50):
        if len(sender.sent) >= 2:
            break
        await asyncio.sleep(0)
    assert "Denied" in _json(sender, len(sender.sent) - 1), "the retry landed the answer"


async def test_admin_confirmation_rechecks_each_click_before_settlement() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    prompt = _prompt().model_copy(update={"admin": True})
    prepare = AsyncMock(
        return_value=prompt.model_copy(update={"detail_lines": ("List price: verified",)})
    )
    check = AsyncMock(return_value="Current admin permission required.")
    waiting = asyncio.create_task(
        cards.hook(
            conversation_id=CONVERSATION_ID,
            service_url=SERVICE_URL,
            prepare_admin=prepare,
            check_admin=check,
        )(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not sender.sent:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        body = _json(sender, 0)
        assert '"title":"Confirm"' in body
        assert "List price: verified" in body
        token = re.search(r'"token":"([^"]+)"', body)
        assert token is not None
        denied = await cards.on_action(_click(token.group(1), "approve", AAD_OBJECT_ID))
        assert denied.value == "Current admin permission required."
        assert len(sender.sent) == 1
        assert not waiting.done()
        check.return_value = None
        await cards.on_action(_click(token.group(1), "approve", AAD_OBJECT_ID))
        assert isinstance(await asyncio.wait_for(waiting, timeout=1), ApprovedConfirmation)
        assert check.await_count == 2
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_admin_confirmation_without_a_verifier_posts_nothing() -> None:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    prompt = _prompt().model_copy(update={"admin": True})
    assert (
        await cards.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL)(prompt)
        == "denied"
    )
    assert not sender.sent


@pytest.mark.parametrize("case", ["expired", "exception", "wrong_requester", "legacy"])
async def test_admin_await_cannot_bypass_requester_expiry_or_fail_closed(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.posted_controls import lifecycle

    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
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
            conversation_id=CONVERSATION_ID,
            service_url=SERVICE_URL,
            prepare_admin=AsyncMock(return_value=prompt),
            check_admin=check,
        )(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not sender.sent:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        token = re.search(r'"token":"([^"]+)"', _json(sender, 0))
        assert token is not None
        response = await cards.on_action(
            _click(
                token.group(1),
                "approve",
                "5c3f4a3b-4485-43d2-a79c-5a339949241a"
                if case == "wrong_requester"
                else AAD_OBJECT_ID,
            )
        )
        if case == "legacy":
            assert isinstance(await asyncio.wait_for(waiting, 1), ApprovedConfirmation)
            check.assert_not_awaited()
        else:
            assert response.value
            assert len(sender.sent) == 1
            assert not waiting.done()
            if case == "wrong_requester":
                check.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_clicks_check_the_cached_owners_and_read_again_only_for_a_yes(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Clicking a card repeatedly must not cost a Graph owner read each time."""
    from daimon.core import confirmation

    team = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"
    owners = AsyncMock(return_value=frozenset({AAD_OBJECT_ID}))
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    runtime = MagicMock(
        team_owners=owners, group_members=GroupMembersCache(), sessionmaker=db_session_factory
    )
    runtime.settings.teams.tenant_id = ENTRA_TENANT_ID
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    runtime.settings.teams.admin_team_id = team
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    tenant, account = uuid.uuid4(), uuid.uuid4()
    TeamsConfirmationCards(FakeSender()).hook(
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        runtime=runtime,
        tenant_id=tenant,
        account_id=account,
    )
    kwargs = factory.call_args.kwargs
    assert (kwargs["tenant_id"], kwargs["account_id"]) == (tenant, account), "the turn's ids"
    check = kwargs["current_admin"]
    for _ in range(25):
        assert await check(OTHER_AAD_OBJECT_ID) is False, "a non-owner is no admin"
    assert owners.await_count == 1, "a cached no needs no further read"
    assert await check(AAD_OBJECT_ID) is True, "an owner's yes is confirmed"
    assert owners.await_count == 2, "only a yes is read again"
    owners.return_value = frozenset()
    assert await check(AAD_OBJECT_ID) is False, "a revoked owner counts at once, cache or not"


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired", "stopped"])
def test_complete_backend_loss_list_survives_native_card_rendering(state):
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
    rendered = confirmation_adaptive_card(card, prompt).model_dump_json(by_alias=True)
    assert (losses in rendered) == (state in {"pending", "approved"})


@pytest.mark.parametrize("conversation_type", ["personal", "channel", "groupChat", "meeting"])
async def test_private_notice_uses_sdk_endpoint_for_conversation_type(
    conversation_type: str,
) -> None:
    wire = TeamsApiFake()
    client = Client(ClientOptions(token="offline-token", headers={}))
    client.use(wire)
    api = ApiClient(SERVICE_URL, client)
    payload = make_card_action(VERB, "always", user=OTHER_AAD_OBJECT_ID, token="tok_abcdefgh")
    conversation = payload["conversation"]
    assert isinstance(conversation, dict)
    conversation["conversationType"] = conversation_type
    context = ActivityContext.__new__(ActivityContext)
    context.activity = AdaptiveCardInvokeActivity.model_validate(payload)
    context.api = api
    context.conversation_ref = ConversationReference(
        channel_id="msteams",
        service_url=SERVICE_URL,
        bot=Account(id="offline-bot"),
        conversation=ConversationAccount(id=CONVERSATION_ID),
    )
    await TeamsConfirmationCards(FakeSender())._notify_clicker(context, "private refusal")
    assert len(wire.activity_requests) == 1
    request = wire.activity_requests[0]
    assert ("isTargetedActivity=true" in request.url) is (conversation_type != "personal")
    assert request.body["attachments"]
    assert "private refusal" in json.dumps(request.body["attachments"])


async def test_runtime_hook_binds_pin_cards_to_the_admitted_origin_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from daimon.core import confirmation
    from mux.contracts.ids import ChannelRef

    runtime = MagicMock(
        team_owners=AsyncMock(return_value=frozenset()), group_members=GroupMembersCache()
    )
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    runtime.settings.teams.admin_team_id = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    tenant, account = uuid.uuid4(), uuid.uuid4()

    def hook(admission):
        TeamsConfirmationCards(FakeSender()).hook(
            conversation_id=CONVERSATION_ID,
            service_url=SERVICE_URL,
            runtime=runtime,
            admission=admission,
            tenant_id=tenant,
            account_id=account,
        )
        return factory.call_args.kwargs["pin_channel"]

    assert hook(SimpleNamespace(origin_channel_id="19:origin@thread.tacv2")) == ChannelRef(
        tenant_id=str(tenant), platform="teams", channel_id="19:origin@thread.tacv2"
    )
    assert hook(None) is None, "no admitted channel, no pin card"


async def test_a_card_still_open_reminds_with_an_at_mention_of_its_requester(monkeypatch) -> None:
    """ROOT 2: a long card pings its requester once, as an @mention Teams delivers."""
    from daimon.core.posted_controls import lifecycle

    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.02)
    monkeypatch.setattr(lifecycle, "REMIND_MIN_LEFT_S", 0.05)
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender)
    hook = cards.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL)
    assert await hook(_prompt(expires_in=timedelta(seconds=0.3))) == "expired"
    reminders = [
        activity
        for _conversation, activity, _url in sender.sent
        if activity.attachments and "still waiting" in str(activity.attachments[0].content.body)
    ]
    (reminder,) = reminders
    [card] = [a.content for a in reminder.attachments]
    [block] = card.body
    assert block.text.endswith("This is still waiting for your click."), "words in a neutral card"
    assert reminder.text is not None and reminder.text.startswith("<at>")
    mentions = [
        entity for entity in reminder.entities or [] if getattr(entity, "type", None) == "mention"
    ]
    (mention,) = mentions
    assert mention.mentioned.id == AAD_OBJECT_ID
    assert mention.text in reminder.text
