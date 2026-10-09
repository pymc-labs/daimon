"""Discord connection card payloads."""

from __future__ import annotations

from typing import Literal

import discord
import pytest
from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
from daimon.core.github_connect_cards import build_connect_card


@pytest.mark.parametrize("variant", ["A", "B", "B_NO_FOOTER"])
def test_connect_embed_variants(variant: Literal["A", "B", "B_NO_FOOTER"]) -> None:
    card = build_connect_card(
        agent_name="ResearchBot",
        identity_enabled=True,
        avatar_url="https://mcp.test/avatars/research.png",
        public_base_url="https://mcp.test",
        variant=variant,
    )
    payload = connect_embed(card).to_dict()
    assert payload["author"] == {
        "name": "ResearchBot",
        "icon_url": "https://mcp.test/avatars/research.png",
    }
    assert payload["title"] == "Connect GitHub"
    assert payload["thumbnail"]["url"] == "https://mcp.test/web/github-mark.png"
    assert payload["color"] == 0x0C1F40
    assert payload["description"].startswith("Pick repos ResearchBot can use.")
    assert ("Default access: Read and write" in payload["description"]) == (variant != "A")
    assert ("footer" in payload) == (variant == "B")
    assert " · " not in str(payload)


def test_connect_embed_escapes_agent_name_markdown() -> None:
    name = "*Research_Bot*"
    card = build_connect_card(
        agent_name=name,
        identity_enabled=True,
        avatar_url=None,
        public_base_url="https://mcp.test",
    )
    embed = connect_embed(card)
    assert embed.description == discord.utils.escape_markdown(f"Pick repos {name} can use.")
