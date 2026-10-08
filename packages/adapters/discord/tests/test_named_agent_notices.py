"""Named-agent refusal cards keep copy, structure, and switch actions stable."""

from __future__ import annotations

import re

import discord
import pytest
from daimon.adapters.discord.named_agent_notices import build_named_agent_notice
from daimon.adapters.discord.theme import COLOR_RED
from daimon.adapters.discord.thread_handoff import CUSTOM_ID_TEMPLATE
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
def test_notice_card_has_separate_copy_and_only_thread_has_switch(
    kind: str, title: str, detail: str
) -> None:
    err = NamedAgentRefused(
        kind=kind,  # type: ignore[arg-type]
        current_name="Daimon",
        named_name="Planner",
        hand_over_agent_id="ag_planner" if kind == "thread" else None,
        hand_over_agent_name="Planner" if kind == "thread" else None,
    )
    view = build_named_agent_notice(err)
    assert isinstance(view, discord.ui.LayoutView)
    container = view.to_components()[0]
    assert container["type"] == 17
    assert container["accent_color"] == COLOR_RED
    children = container["components"]
    assert children[:3] == [
        {"type": 10, "content": f"## {title}"},
        {"type": 14, "divider": True, "spacing": 1},
        {"type": 10, "content": detail},
    ]
    rows = [child for child in children if child["type"] == 1]
    if kind == "thread":
        assert rows[0]["components"][0]["label"] == "Switch to Planner"
        assert rows[0]["components"][0]["custom_id"] == "tho:ag_planner"
        assert re.fullmatch(CUSTOM_ID_TEMPLATE, rows[0]["components"][0]["custom_id"])
    else:
        assert rows == []
