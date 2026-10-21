"""Executable record: progress plans work on Discord, Slack and Teams alike.

One deliberate asymmetry with thread participation, whose cascade the switch
shares: participation refuses Slack callers, while progress plans cover Slack
too. A Slack thread timestamp is unique only within its channel, so the
switch keys a Slack thread as ``<channel>:<thread ts>`` and refuses a bare
timestamp. All three cards draw the plan from `format_plan_lines`, so the
checklist reads the same everywhere and only the markup differs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from daimon.adapters.discord import embed as discord_embed
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.progress_plans import (
    _set_progress_plans_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.slack import blockkit
from daimon.adapters.teams import card as teams_card
from daimon.core.stores.domain import Role
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.state import ToolUseBlock, TurnState
from fastmcp.exceptions import ToolError
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
_PLAN = ["✔️ Load the data", "🖋️ Fit the model", "\u2003\u2002Write it up"]


def _discord() -> list[str]:
    state = discord_embed.update_activity(discord_embed.EmbedState(started_at=100.0), _TURN)
    description = discord_embed.to_embed_data(state, now=165.0).description
    return description.splitlines() if description else []


def _slack() -> list[str]:
    state = blockkit.update_activity(blockkit.State(started_at=100.0), _TURN)
    sections = [b for b in blockkit.to_blocks(state, now=165.0) if b["type"] == "section"]
    return sections[1]["text"]["text"].splitlines() if len(sections) > 1 else []


def _teams() -> list[str]:
    state = teams_card.on_activity(teams_card.CardState(started_at=100.0), _TURN)
    activity = teams_card.status_card(state, now=165.0, cancel_key="k")
    body: list[dict[str, Any]] = activity.model_dump(by_alias=True)["attachments"][0]["content"][
        "body"
    ]
    texts = [block["text"] for block in body if block["type"] == "TextBlock"]
    return texts[1].split("\n\n") if texts[1].startswith("✔️") else []


@pytest.mark.parametrize("on", [True, False])
def test_every_card_draws_the_same_plan_only_when_on(on: bool) -> None:
    """The turn's switch decides; the three cards then agree word for word."""
    observation = TurnObservation(async_sessionmaker(), None, "discord", progress_plans=on)
    with observation.activate():
        cards = {"discord": _discord(), "slack": _slack(), "teams": _teams()}
    expected = _PLAN if on else []
    assert cards == dict.fromkeys(cards, expected), "each card shows the same checklist, or none"


async def test_slack_is_covered_and_a_bare_thread_timestamp_is_refused() -> None:
    """Unlike participation, a Slack caller gets past the platform check."""
    caller = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="slack",
        is_admin=False,
    )
    runtime = MagicMock()
    runtime.session_factory = MagicMock(side_effect=AssertionError("no DB read may happen"))
    with pytest.raises(ToolError, match="channel_id"):
        await _set_progress_plans_impl(runtime, caller, "on", "1700000000.000100", None)
