"""Discord connection card payloads."""

from __future__ import annotations

from typing import Literal

import pytest
from daimon.adapters.discord.agent_setup.github_connect_card import connect_embed
from daimon.core.github_connect_cards import build_connect_card


@pytest.mark.parametrize("variant", ["A", "B"])
def test_connect_embed_variants(variant: Literal["A", "B"]) -> None:
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
    assert ("Default access: Read and write" in payload["description"]) == (variant == "B")
    assert ("footer" in payload) == (variant == "B")
    assert " · " not in str(payload)
