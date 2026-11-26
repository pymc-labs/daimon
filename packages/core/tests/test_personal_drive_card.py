"""Every personal Drive read asks its requester on a card, until they allow it for the
conversation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from daimon.core.confirmation import ConfirmationPrompt, prompt_for_tool_call
from daimon.core.posted_controls.confirmation import (
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_custom_id,
    parse_confirmation_custom_id,
)
from daimon.core.session_allowances import allow
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.approvals import interactive_decider
from daimon.core.turn.posture import ToolConfirmationResult

TRUSTED = frozenset({DAIMON_SERVER_NAME})
CALL = ToolCall(
    tool_use_id="t1",
    server_name=DAIMON_SERVER_NAME,
    tool_name="data_my_drive",
    input={"query": "budget", "origin_context_id": "o1"},
)


async def test_each_read_asks_until_the_requester_allows_it_for_the_conversation() -> None:
    card = AsyncMock(return_value="approved")

    async def decide(session: str) -> ToolConfirmationResult:
        return await interactive_decider(
            ToolSafetyPolicy(enabled=True),
            requester_platform_user_id="u1",
            confirm=card,
            trusted_servers=TRUSTED,
            session_key=session,
        )(CALL)

    assert (await decide("s1")).allow
    assert (await decide("s1")).allow
    assert card.await_count == 2, "an approval once covers that read only"
    assert card.await_args is not None
    prompt: ConfirmationPrompt = card.await_args.args[0]
    assert prompt.consequence and "Everyone" in prompt.consequence, "the room sees the result"
    assert prompt.requester_platform_user_id == "u1"

    pending = build_confirmation_card(prompt, state="pending", token="tok12345")
    assert pending.allow_session
    blocks = str(build_confirmation_blocks(pending, prompt=prompt))
    assert confirmation_custom_id("tok12345", "session") in blocks
    assert parse_confirmation_custom_id(confirmation_custom_id("tok12345", "session")) == (
        "tok12345",
        "approved",
    ), "a requester-only answer, like Approve"

    assert prompt.session_allow
    allow(prompt.session_allow)  # what the adapter does on "Allow for this conversation"
    assert (await decide("s1")).allow
    assert card.await_count == 2, "allowed for the rest of this conversation: no card"
    assert (await decide("s2")).allow
    assert card.await_count == 3, "another conversation asks again"


def test_the_opt_out_card_says_what_the_parsed_call_does() -> None:
    for on, verb in (("true", "Let"), ("0", "Stop"), (True, "Let")):
        call = ToolCall(
            tool_use_id="t2",
            server_name=DAIMON_SERVER_NAME,
            tool_name="data_personal_reads",
            input={"part": "drive", "on": on, "origin_context_id": "o1"},
        )
        prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
        assert prompt.title.startswith(verb), on
