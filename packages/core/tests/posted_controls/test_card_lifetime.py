"""Approval cards: up to 24h, never past their turn, one reminder, plain endings (ROOT 2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt, prompt_for_tool_call
from daimon.core.posted_controls import lifecycle
from daimon.core.posted_controls.confirmation import (
    ENDED_UNANSWERED_FOOTER,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import (
    STILL_WAITING_MESSAGE,
    PostedConfirmations,
    wait_local_confirmation,
)
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn.approvals import CARD_TURN_MARGIN, card_timeout, interactive_decider
from daimon.core.turn.ceiling import TURN_CEILING_S, current_turn_deadline
from pydantic import ValidationError

NOW = datetime(2026, 10, 23, 13, tzinfo=UTC)
CALL = ToolCall(
    tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
)


def test_a_card_may_wait_up_to_a_day_and_no_longer():
    assert ToolSafetyPolicy(confirmation_timeout_s=86_400).confirmation_timeout_s == 86_400
    with pytest.raises(ValidationError):
        ToolSafetyPolicy(confirmation_timeout_s=86_401)


def test_a_card_never_promises_a_time_past_its_turn():
    day = ToolSafetyPolicy(confirmation_timeout_s=86_400)
    # Outside a turn the policy alone decides.
    assert card_timeout(day, now=NOW) == timedelta(days=1)
    deadline = NOW + timedelta(seconds=TURN_CEILING_S)
    token = current_turn_deadline.set(deadline)
    try:
        assert card_timeout(day, now=NOW) == deadline - CARD_TURN_MARGIN - NOW
        # A short policy still wins inside a long turn.
        short = ToolSafetyPolicy(confirmation_timeout_s=60)
        assert card_timeout(short, now=NOW) == timedelta(seconds=60)
        # A turn already at its end gives no time, never a negative one.
        assert card_timeout(day, now=deadline + timedelta(minutes=1)) == timedelta(0)
    finally:
        current_turn_deadline.reset(token)


async def test_the_posted_card_shows_the_turns_end_not_the_policys_day():
    seen: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        seen.append(prompt)
        return "denied"

    decide = interactive_decider(
        ToolSafetyPolicy(enabled=True, confirmation_timeout_s=86_400),
        requester_platform_user_id="U1",
        confirm=confirm,
        now=lambda: NOW,
    )
    deadline = NOW + timedelta(minutes=40)
    token = current_turn_deadline.set(deadline)
    try:
        result = await decide(CALL)
    finally:
        current_turn_deadline.reset(token)
    assert not result.allow
    (prompt,) = seen
    assert prompt.expires_at == deadline - CARD_TURN_MARGIN


def _prompt(seconds: float) -> ConfirmationPrompt:
    now = datetime.now(UTC)
    return prompt_for_tool_call(
        CALL, requester_platform_user_id="U1", now=now, timeout=timedelta(seconds=seconds)
    )


class _Card:
    def __init__(self, prompt: ConfirmationPrompt) -> None:
        self.prompt = prompt


async def test_a_card_still_open_pings_its_requester_once(monkeypatch):
    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.05)
    reminded: list[str] = []
    retired: list[str] = []

    async def remind() -> None:
        reminded.append(STILL_WAITING_MESSAGE)

    async def retire(_card: _Card, state: str) -> None:
        retired.append(state)

    controls = PostedConfirmations[_Card]()
    prompt = _prompt(0.3)
    answer = await controls.confirm(
        prompt,
        post=lambda token: asyncio.sleep(0, _Card(prompt)),
        retire=retire,
        post_errors=(),
        remind=remind,
    )
    assert answer == "expired" and retired == ["expired"]
    assert reminded == ["This is still waiting for your click."]


@pytest.mark.parametrize("case", ["answered_first", "short_card", "no_hook"])
async def test_no_reminder_when_answered_short_or_unsupported(monkeypatch, case):
    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.1)
    reminded: list[int] = []

    async def remind() -> None:
        reminded.append(1)

    async def retire(_state: str) -> None:
        return None

    future: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    if case == "answered_first":
        asyncio.get_running_loop().call_later(0.02, future.set_result, "approved")
    answer = await wait_local_confirmation(
        _prompt(0.05 if case == "short_card" else 0.4),
        future,
        stop=lambda: None,
        retire=retire,
        remind=None if case == "no_hook" else remind,
    )
    assert answer == ("approved" if case == "answered_first" else "expired")
    assert reminded == []


async def test_a_failed_reminder_never_ends_the_wait(monkeypatch):
    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.02)

    async def remind() -> None:
        raise RuntimeError("thread gone")

    async def retire(_state: str) -> None:
        return None

    future: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_later(0.1, future.set_result, "approved")
    answer = await wait_local_confirmation(
        _prompt(0.5), future, stop=lambda: None, retire=retire, remind=remind
    )
    assert answer == "approved"


def test_an_expired_card_says_plainly_that_nobody_clicked():
    prompt = _prompt(60)
    expired = build_confirmation_card(prompt, state="expired")
    assert expired.footer == ENDED_UNANSWERED_FOOTER
    assert expired.footer == "This request ended before anyone clicked. Ask again to redo it."
    # A stopped card (a cancelled turn, or one retired after its Approve) claims nothing.
    assert build_confirmation_card(prompt, state="stopped").footer is None
