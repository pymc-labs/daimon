"""Executable record: Discord, Slack and Teams draw the same in-progress status card.

All three fold the turn state through `daimon.core.turn.status_lines`, so the
headline, tool lines, draft and elapsed time are the same words and only the markup differs:
Teams draws the tool lines as a monospace block and the draft as subtle text,
since an Adaptive Card TextBlock renders no code fence or quote. Under the
headline every card reads the latest draft lines, then the plan, then the tool
lines (`CARD_SECTIONS`). One deliberate asymmetry: a Discord embed has a bar
color and the others have none.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.discord import embed as discord_embed
from daimon.adapters.slack import blockkit
from daimon.adapters.teams import card as teams_card
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock, TurnState
from sqlalchemy.ext.asyncio import async_sessionmaker

_CONTENT: list[ContentBlock] = [
    TextBlock(kind="text", text="Checking."),
    ToolUseBlock(
        kind="tool_use",
        id="tu_1",
        type="agent.tool_use",
        name="read",
        input={},
        status="complete",
    ),
    ToolUseBlock(
        kind="tool_use",
        id="tu_2",
        type="agent.mcp_tool_use",
        name="search_issues",
        input={},
        mcp_server_name="tracker",
    ),
]
_TURN = TurnState(content=_CONTENT, finished_tool_ids=("tu_1",))
_DRAFT = "Looking through the open issues now"
_PLAN_CALL = ToolUseBlock(
    kind="tool_use",
    id="tu_plan",
    type="agent.mcp_tool_use",
    name="update_plan",
    input={"steps": [{"step": "Read the issues", "status": "in_progress"}]},
    mcp_server_name=DAIMON_SERVER_NAME,
    status="complete",
)


def _discord_lines(turn: TurnState = _TURN) -> list[str]:
    state = discord_embed.update(
        discord_embed.EmbedState(started_at=100.0),
        discord_embed.EmbedEvent(kind="message", label=_DRAFT),
    )
    state = discord_embed.update_activity(state, turn)
    data = discord_embed.to_embed_data(state, now=165.0)
    return [
        data.title,
        *(line.removeprefix("> ") for line in data.description.splitlines()),
        *(
            line
            for name, value in discord_embed.embed_fields(data)
            for line in (name, *value.replace("`", "").splitlines())
        ),
        data.footer or "",
    ]


def _slack_lines(turn: TurnState = _TURN) -> list[str]:
    state = blockkit.update(
        blockkit.State(started_at=100.0), blockkit.EmbedEvent(kind="message", label=_DRAFT)
    )
    state = blockkit.update_activity(state, turn)
    blocks = blockkit.to_blocks(state, now=165.0)
    lines: list[str] = []
    for block in blocks:
        if block["type"] == "section":
            text = block["text"]["text"].strip("*")
            lines.extend(line.removeprefix("> ") for line in text.splitlines())
        elif block["type"] == "context":
            text = block["elements"][0]["text"]
            if text.startswith(("*Plan*", "*Details*")):
                lines.extend(text.replace("*", "").replace("`", "").splitlines())
            else:
                lines.append(text)
    return lines


def _teams_lines(turn: TurnState = _TURN) -> list[str]:
    state = teams_card.on_message(teams_card.CardState(started_at=100.0), _DRAFT)
    state = teams_card.on_activity(state, turn)
    activity = teams_card.status_card(state, now=165.0, cancel_key="k")
    body: list[dict[str, Any]] = activity.model_dump(by_alias=True)["attachments"][0]["content"][
        "body"
    ]
    blocks = [block["text"] for block in body if block["type"] == "TextBlock"]
    return [line.replace("**", "") for text in blocks for line in text.split("\n\n")]


def test_teams_shows_the_same_status_words_as_slack() -> None:
    assert _teams_lines() == _slack_lines(), "the Teams card must say what the Slack card says"


def test_discord_and_slack_show_the_same_status_words() -> None:
    assert _discord_lines() == _slack_lines(), (
        "the two cards must say the same thing about the same turn, markup aside"
    )


def test_every_card_reads_the_updates_then_the_plan_then_the_tool_lines() -> None:
    """The order comes from core, so no card can lift the plan above the latest updates."""
    turn = TurnState(content=[*_CONTENT, _PLAN_CALL], finished_tool_ids=("tu_1",))
    observation = TurnObservation(async_sessionmaker(), None, "discord", progress_plans=True)
    with observation.activate():
        cards = {"discord": _discord_lines(turn), "slack": _slack_lines(turn)}
        cards["teams"] = _teams_lines(turn)
    expected = [
        "Working on it…",
        _DRAFT,
        "Plan",
        "⏳ Read the issues",
        "Details",
        "☑️ Read a file",
        "🔍 Search issues (tracker)",
        "1m 5s",
    ]
    assert cards == dict.fromkeys(cards, expected), "headline, updates, plan, then tool lines"


def test_only_discord_has_a_bar_color() -> None:
    data = discord_embed.to_embed_data(
        discord_embed.update_activity(discord_embed.EmbedState(), _TURN)
    )
    assert data.color, "a running Discord card has a bar color"
    blocks = blockkit.to_blocks(blockkit.update_activity(blockkit.State(), _TURN), now=None)
    assert all("color" not in block for block in blocks), "Block Kit has no bar color"
