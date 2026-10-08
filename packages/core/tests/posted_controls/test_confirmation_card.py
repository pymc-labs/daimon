"""The platform-neutral confirmation card and the in-process click rendezvous."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import (
    CONFIRMATION_TIMEOUT,
    MAX_DETAIL_CHARS,
    ConfirmationPrompt,
    PendingConfirmations,
    no_confirmation_surface,
    prompt_for_tool_call,
)
from daimon.core.posted_controls.confirmation import (
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_custom_id,
    parse_confirmation_custom_id,
)
from daimon.core.tool_safety import ToolCall

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _prompt() -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1",
        server_name="linear",
        tool_name="create_issue",
        input={"title": "Bug", "team": "ENG"},
    )
    return prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)


def test_the_prompt_names_the_server_the_tool_and_the_exact_input() -> None:
    prompt = _prompt()
    assert prompt.title == "Approve a write to linear?"
    assert prompt.fields == (("Tool", "create_issue"), ("Server", "linear"))
    assert prompt.detail == '{\n  "team": "ENG",\n  "title": "Bug"\n}'
    assert prompt.expires_at == _NOW + CONFIRMATION_TIMEOUT


def test_a_long_input_is_cut_with_a_marker() -> None:
    call = ToolCall(
        tool_use_id="tu_1", server_name="s", tool_name="write_doc", input={"body": "x" * 5000}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)
    assert prompt.detail is not None
    assert prompt.detail.endswith("… (truncated)")
    assert len(prompt.detail) < MAX_DETAIL_CHARS + 20


def test_a_naive_expiry_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ConfirmationPrompt(
            title="t", requester_platform_user_id="U1", expires_at=datetime(2026, 1, 1)
        )


@pytest.mark.parametrize(
    ("state", "mark"),
    [("pending", "✋"), ("approved", "✅"), ("denied", "🛡️"), ("expired", "⌛")],
)
def test_every_state_leads_with_its_mark(state: str, mark: str) -> None:
    token = "tok_abcdefgh" if state == "pending" else None
    card = build_confirmation_card(_prompt(), state=state, token=token)  # type: ignore[arg-type]
    assert card.headline.startswith(mark)
    assert card.fields == _prompt().fields


def test_only_a_pending_card_carries_a_token() -> None:
    with pytest.raises(ValueError):
        build_confirmation_card(_prompt(), state="pending")
    with pytest.raises(ValueError):
        build_confirmation_card(_prompt(), state="approved", token="tok_abcdefgh")


def test_slack_blocks_carry_buttons_only_while_pending() -> None:
    prompt = _prompt()
    pending = build_confirmation_blocks(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt=prompt
    )
    actions = [b for b in pending if b["type"] == "actions"]
    assert [e["action_id"] for e in actions[0]["elements"]] == [
        "dcf:tok_abcdefgh:approve",
        "dcf:tok_abcdefgh:deny",
    ]
    assert "<@U1>" in pending[-1]["elements"][0]["text"]
    approved = build_confirmation_blocks(
        build_confirmation_card(prompt, state="approved", answered_by_platform_user_id="U1"),
        prompt=prompt,
    )
    assert not [b for b in approved if b["type"] == "actions"]


def test_slack_tool_arguments_stay_in_code_formatting() -> None:
    prompt = ConfirmationPrompt(
        title="Approve a write?",
        detail='{"body": "*do not render as emphasis*"}',
        requester_platform_user_id="U1",
        expires_at=_NOW,
    )
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    blocks = build_confirmation_blocks(card, prompt=prompt)
    assert any(block.get("text", {}).get("text") == f"```{prompt.detail}```" for block in blocks)


def test_button_ids_round_trip() -> None:
    assert parse_confirmation_custom_id(confirmation_custom_id("tok_abcdefgh", "approve")) == (
        "tok_abcdefgh",
        "approved",
    )
    assert parse_confirmation_custom_id(confirmation_custom_id("tok_abcdefgh", "deny")) == (
        "tok_abcdefgh",
        "denied",
    )
    assert parse_confirmation_custom_id("ztc:abc") is None


async def test_the_default_hook_refuses() -> None:
    assert await no_confirmation_surface(_prompt()) == "denied"


async def test_pending_confirmations_resolve_once() -> None:
    pending = PendingConfirmations()
    token, future = pending.open()
    waiter = asyncio.create_task(pending.wait(token, future, timeout_s=5))
    assert pending.resolve(token, "approved") is True
    assert pending.resolve(token, "denied") is False
    assert await waiter == "approved"


async def test_pending_confirmations_expire() -> None:
    pending = PendingConfirmations()
    token, future = pending.open()
    assert await pending.wait(token, future, timeout_s=0.01) == "expired"
    assert pending.resolve(token, "approved") is False
