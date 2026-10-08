"""Named-agent refusal Block Kit uses approved copy and the existing switch action."""

from __future__ import annotations

import pytest
from daimon.adapters.slack.named_agent_notices import build_named_agent_blocks
from daimon.adapters.slack.thread_handoff import HAND_OVER_ACTION_ID
from daimon.core.turn.errors import NamedAgentRefused


@pytest.mark.parametrize(
    ("kind", "title", "detail"),
    [
        ("two", "You named two agents.", "Use one name, like `@daimon planner: …`"),
        ("unavailable", "That agent isn't available.", "Ask again without the name."),
        (
            "setup",
            "You're setting up Daimon here.",
            "Start a new message in the channel to ask Planner.",
        ),
        (
            "thread",
            "This thread is with Daimon.",
            "Start a new message in the channel to ask Planner.",
        ),
        ("own", "Only Daimon answers here.", "Ask again without the other name."),
    ],
)
def test_blocks_have_approved_copy_and_only_thread_has_switch(
    kind: str, title: str, detail: str
) -> None:
    err = NamedAgentRefused(
        kind=kind,  # type: ignore[arg-type]
        current_name="Daimon",
        named_name="Planner",
        hand_over_agent_id="ag_planner" if kind == "thread" else None,
        hand_over_agent_name="Planner" if kind == "thread" else None,
    )
    blocks = build_named_agent_blocks(err)
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": title}}
    assert blocks[1] == {"type": "section", "text": {"type": "mrkdwn", "text": detail}}
    actions = [block for block in blocks if block["type"] == "actions"]
    if kind == "thread":
        button = actions[0]["elements"][0]
        assert button["action_id"] == HAND_OVER_ACTION_ID
        assert button["value"] == "ag_planner"
        assert button["text"]["text"] == "Switch to Planner"
    else:
        assert actions == []


def test_agent_names_are_escaped_in_mrkdwn() -> None:
    err = NamedAgentRefused(kind="thread", current_name="Daimon", named_name="<@U123>")
    blocks = build_named_agent_blocks(err)
    assert "&lt;@U123&gt;" in blocks[1]["text"]["text"]
    assert "<@U123>" not in blocks[1]["text"]["text"]
