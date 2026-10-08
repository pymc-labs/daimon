"""Private GitHub cards use native Discord and Slack layouts."""

from __future__ import annotations

import uuid

from daimon.adapters.mcp.tools.github_request_delivery import (
    _discord_embed,
    _discord_view,
    _slack_blocks,
)
from daimon.core.github_request_cards import RequestCard


def test_request_card_has_state_edge_fields_and_separate_actions() -> None:
    card = RequestCard(
        "Let ResearchBot use a repo that isn't connected yet?\nCan: read only.",
        "Connect and add",
        ("Decline", "Hide for me"),
    )
    embed = _discord_embed(card)
    assert embed.title == "Let ResearchBot use a repo that isn't connected yet?"
    assert [(field.name, field.value) for field in embed.fields] == [("Can", "read only.")]
    assert embed.footer.text == "GitHub on Daimon"
    assert embed.color is not None
    view = _discord_view(card, request_id=uuid.uuid4(), link_url=None)
    assert len(view.to_components()) == 1

    blocks = _slack_blocks(card, request_id=uuid.uuid4(), link_url=None)
    assert [block["type"] for block in blocks] == [
        "section",
        "section",
        "divider",
        "actions",
        "context",
    ]
    assert "repo that isn't connected yet" in str(blocks)
