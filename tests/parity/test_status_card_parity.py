"""Executable record: Discord, Slack and Teams draw the same in-progress status card.

All three fold the turn state through `daimon.core.turn.status_lines`, so the
headline, tool lines and draft are the same words and only the markup differs:
Teams draws the tool lines as a monospace block and the draft as subtle text,
since an Adaptive Card TextBlock renders no code fence or quote. One
deliberate asymmetry: a Discord embed has a bar color and the others have none.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.discord import embed as discord_embed
from daimon.adapters.slack import blockkit
from daimon.adapters.teams import card as teams_card
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock, TurnState

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


def _discord_text() -> str:
    state = discord_embed.update(
        discord_embed.EmbedState(started_at=100.0),
        discord_embed.EmbedEvent(kind="message", label=_DRAFT),
    )
    state = discord_embed.update_activity(state, _TURN)
    return discord_embed.to_embed_data(state, now=165.0).description


def _slack_text() -> str:
    state = blockkit.update(
        blockkit.State(started_at=100.0), blockkit.EmbedEvent(kind="message", label=_DRAFT)
    )
    state = blockkit.update_activity(state, _TURN)
    blocks = blockkit.to_blocks(state, now=165.0)
    return "\n".join(block["text"]["text"] for block in blocks if block["type"] == "section")


def _teams_lines() -> list[str]:
    state = teams_card.on_message(teams_card.CardState(started_at=100.0), _DRAFT)
    state = teams_card.on_activity(state, _TURN)
    activity = teams_card.status_card(state, now=165.0, cancel_key="k")
    body: list[dict[str, Any]] = activity.model_dump(by_alias=True)["attachments"][0]["content"][
        "body"
    ]
    blocks = [block["text"] for block in body if block["type"] == "TextBlock"]
    return [line.replace("**", "") for text in blocks for line in text.split("\n\n")]


def _slack_lines() -> list[str]:
    lines = _slack_text().replace("*", "").splitlines()
    return [line.removeprefix("> ") for line in lines if line != "```"]


def test_teams_shows_the_same_status_words_as_slack() -> None:
    assert _teams_lines() == _slack_lines(), "the Teams card must say what the Slack card says"


def test_discord_and_slack_show_the_same_status_words() -> None:
    assert _discord_text().replace("**", "*") == _slack_text(), (
        "the two cards must say the same thing about the same turn, markup aside"
    )


def test_only_discord_has_a_bar_color() -> None:
    data = discord_embed.to_embed_data(
        discord_embed.update_activity(discord_embed.EmbedState(), _TURN)
    )
    assert data.color, "a running Discord card has a bar color"
    blocks = blockkit.to_blocks(blockkit.update_activity(blockkit.State(), _TURN), now=None)
    assert all("color" not in block for block in blocks), "Block Kit has no bar color"
