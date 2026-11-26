"""A shared connection change's card names what it acts on, read from the store."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from daimon.core.confirmation import SharedConnectionCard, prompt_for_tool_call
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.approvals import SHARED_CONNECTION_UNREADABLE, interactive_decider

TURN_ON = ToolCall(
    tool_use_id="t1",
    server_name=DAIMON_SERVER_NAME,
    tool_name="change_shared_connection_tool",
    input={
        "action": "turn_on",
        "connection": "c1",
        "tool": "send",
        "seen": "d",
        "origin_context_id": "o",
    },
)
URL = "https://crm.example/mcp/" + "a" * 80
CARD = SharedConnectionCard("crm", URL, tool="send", description="Sends a note.", writes=True)


def test_turn_on_names_the_tool_its_server_and_what_callers_are_told() -> None:
    prompt = prompt_for_tool_call(
        TURN_ON, requester_platform_user_id="u1", now=datetime.now(UTC), shared_connection=CARD
    )
    assert prompt.title == "Turn on crm_send?"
    assert prompt.consequence == "It changes things. Callers are told: Sends a note."
    assert f"Server: {URL}" in prompt.detail_lines, "the whole address, not a cut one"
    assert "Tool: crm_send" in prompt.detail_lines


async def test_a_change_that_moved_on_is_refused_without_a_card() -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="u1",
        confirm=card,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=frozenset({"change_shared_connection_tool"}),
        shared_connection_for=AsyncMock(return_value=None),
    )(TURN_ON)
    assert not result.allow and result.deny_message == SHARED_CONNECTION_UNREADABLE
    card.assert_not_awaited()
