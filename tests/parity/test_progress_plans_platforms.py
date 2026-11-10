"""Executable record: progress plans work on Discord, Slack and Teams alike.

All three cards draw the plan from `format_plan_lines`, so the checklist reads
the same everywhere and only the markup differs.
"""

from __future__ import annotations

from typing import Any

import pytest
from daimon.adapters.discord import embed as discord_embed
from daimon.adapters.slack import blockkit
from daimon.adapters.teams import card as teams_card
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.state import ToolUseBlock, TurnState
from sqlalchemy.ext.asyncio import async_sessionmaker

_STEPS = [
    {"step": "Load the data", "status": "completed"},
    {"step": "Fit the model", "status": "in_progress"},
    {"step": "Write it up", "status": "pending"},
]
_TURN = TurnState(
    content=[
        ToolUseBlock(
            kind="tool_use",
            id="tu_plan",
            type="agent.mcp_tool_use",
            name="update_plan",
            input={"steps": _STEPS},
            mcp_server_name=DAIMON_SERVER_NAME,
            status="complete",
        )
    ]
)
_PLAN = ["☑️ Load the data", "⏳ Fit the model", "\u2007\u2007\u2007Write it up"]


def _discord() -> list[str]:
    state = discord_embed.update_activity(discord_embed.EmbedState(started_at=100.0), _TURN)
    plan = discord_embed.to_embed_data(state, now=165.0).plan
    return [line.strip("`") for line in plan.splitlines()] if plan else []


def _slack() -> list[str]:
    state = blockkit.update_activity(blockkit.State(started_at=100.0), _TURN)
    for block in blockkit.to_blocks(state, now=165.0):
        text = block["elements"][0]["text"] if block["type"] == "context" else ""
        if text.startswith("*Plan*"):
            return [line.strip("`") for line in text.splitlines()[1:]]
    return []


def _teams() -> list[str]:
    state = teams_card.on_activity(teams_card.CardState(started_at=100.0), _TURN)
    activity = teams_card.status_card(state, now=165.0, cancel_key="k")
    body: list[dict[str, Any]] = activity.model_dump(by_alias=True)["attachments"][0]["content"][
        "body"
    ]
    texts = [block["text"] for block in body if block["type"] == "TextBlock"]
    return texts[2].split("\n\n") if texts[1] == "Plan" else []


@pytest.mark.parametrize("on", [True, False])
def test_every_card_draws_the_same_plan_only_when_on(on: bool) -> None:
    """The turn's switch decides; the three cards then agree word for word."""
    observation = TurnObservation(async_sessionmaker(), None, "discord", progress_plans=on)
    with observation.activate():
        cards = {"discord": _discord(), "slack": _slack(), "teams": _teams()}
    expected = _PLAN if on else []
    assert cards == dict.fromkeys(cards, expected), "each card shows the same checklist, or none"
