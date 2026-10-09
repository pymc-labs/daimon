"""Shared GitHub connection card copy and platform payloads."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from daimon.core import github_connect_cards as cards


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("ResearchBot", "ResearchBot"),
        ("ghv-prod-1409-d7d94d", None),
        ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", None),
        ("a0eebc999c0b4ef8bb6d6bb9bd380a11", None),
    ],
)
def test_names_on_connect_card(name: str, shown: str | None) -> None:
    card = cards.build_connect_card(
        agent_name=name,
        identity_enabled=True,
        avatar_url="https://mcp.test/avatars/face.png",
        public_base_url="https://mcp.test",
    )
    assert card.author_name == (shown or "This agent")
    assert card.description == f"Pick repos {shown or 'this agent'} can use."
    if shown is None:
        assert name not in json.dumps(
            cards.connect_button_blocks("https://mcp.test/secret", card=card)
        )


@pytest.mark.parametrize("variant", ["A", "B", "B_NO_FOOTER"])
def test_variants_render_branded_slack_card(
    variant: Literal["A", "B", "B_NO_FOOTER"],
) -> None:
    card = cards.build_connect_card(
        agent_name="ResearchBot",
        identity_enabled=False,
        avatar_url="https://mcp.test/avatars/agent.png",
        public_base_url="https://mcp.test",
        variant=variant,
    )
    attachment = cards.connect_attachment("https://mcp.test/oauth/github/connect/secret", card=card)
    assert card.author_name == "Daimon"
    assert card.author_icon_url == "https://mcp.test/web/daimon-face.png"
    assert attachment["color"] == "#0C1F40"
    blocks = attachment["blocks"]
    assert blocks[0]["elements"][0]["image_url"] == card.author_icon_url
    assert blocks[1]["text"]["text"] == "Connect GitHub"
    assert blocks[2]["accessory"]["image_url"] == "https://mcp.test/web/github-mark.png"
    actions = next(block for block in blocks if block["type"] == "actions")
    assert actions["elements"][0]["text"]["text"] == "🔗 Connect GitHub"
    assert actions["elements"][0]["style"] == "primary"
    assert card.detail == ("Default access: Read and write" if variant != "A" else None)
    assert card.footer == ("Only you can see the link." if variant == "B" else None)
    for block in blocks:
        if block["type"] != "actions":
            assert "secret" not in json.dumps(block)
            assert " · " not in json.dumps(block)


def test_module_switch_selects_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cards, "VARIANT", "B")
    card = cards.build_connect_card(
        agent_name=None,
        identity_enabled=False,
        avatar_url=None,
        public_base_url="https://mcp.test",
    )
    assert card.variant == "B"


def test_card_clips_long_agent_name() -> None:
    name = "ResearchBot" * 12
    card = cards.build_connect_card(
        agent_name=name,
        identity_enabled=True,
        avatar_url=None,
        public_base_url="https://mcp.test",
    )
    assert card.author_name == name[:80]
    assert card.description == f"Pick repos {name[:80]} can use."
