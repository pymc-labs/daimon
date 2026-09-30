"""Executable record: Discord and Slack draw the same in-progress status card.

Both fold the turn state through `daimon.core.turn.status_lines`, so the
headline, tool lines and draft are the same words and only the bold markup
differs. One deliberate asymmetry: a Discord embed has a bar color and a Block
Kit message has none.
"""

from __future__ import annotations

from daimon.adapters.discord import embed as discord_embed
from daimon.adapters.slack import blockkit
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock

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
_DRAFT = "Looking through the open issues now"


def _discord_text() -> str:
    state = discord_embed.update(
        discord_embed.EmbedState(started_at=100.0),
        discord_embed.EmbedEvent(kind="message", label=_DRAFT),
    )
    state = discord_embed.update_activity(state, _CONTENT)
    return discord_embed.to_embed_data(state, now=165.0).description


def _slack_text() -> str:
    state = blockkit.update(
        blockkit.State(started_at=100.0), blockkit.EmbedEvent(kind="message", label=_DRAFT)
    )
    state = blockkit.update_activity(state, _CONTENT)
    blocks = blockkit.to_blocks(state, now=165.0)
    return "\n".join(block["text"]["text"] for block in blocks if block["type"] == "section")


def test_discord_and_slack_show_the_same_status_words() -> None:
    assert _discord_text().replace("**", "*") == _slack_text(), (
        "the two cards must say the same thing about the same turn, markup aside"
    )


def test_only_discord_has_a_bar_color() -> None:
    data = discord_embed.to_embed_data(
        discord_embed.update_activity(discord_embed.EmbedState(), _CONTENT)
    )
    assert data.color, "a running Discord card has a bar color"
    blocks = blockkit.to_blocks(blockkit.update_activity(blockkit.State(), _CONTENT), now=None)
    assert all("color" not in block for block in blocks), "Block Kit has no bar color"
