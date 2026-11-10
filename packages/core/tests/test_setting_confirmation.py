"""The `change_setting` approval card says the change in the panel's words."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

_CARDS = [
    ({"key": "approvals", "tier": "thread", "value": "off"}, "Approvals: Allow writes for this thread"),
    ({"key": "cost_footer", "tier": "workspace", "value": "hidden"}, "Cost footer: Hidden for all channels"),
    ({"key": "cost_footer", "tier": "thread", "value": "hidden"}, "Cost footer: Hidden for this thread"),
    ({"key": "cost_footer", "tier": "thread", "value": "default"}, "Cost footer: the default for this thread"),
    ({"key": "cost_footer", "tier": "channel", "value": "hidden", "locked": True}, "Cost footer: Hidden, locked for this channel"),
    ({"key": "approvals", "tier": "agent", "value": "off"}, None),
]  # fmt: skip


@pytest.mark.parametrize(("arguments", "line"), _CARDS)
def test_the_agent_tool_card_says_the_change_in_the_panel_words(
    arguments: dict[str, object], line: str | None
) -> None:
    call = ToolCall(
        tool_use_id="s", server_name=DAIMON_SERVER_NAME, tool_name="change_setting",
        input={"origin_context_id": "origin", **arguments},
    )  # fmt: skip
    prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
    assert prompt.consequence == line, f"card for {arguments}"
    assert prompt.detail_lines == (
        (line,) if line else tuple(f"{k.capitalize()}: {v}" for k, v in arguments.items())
    ), f"detail lines for {arguments}"
