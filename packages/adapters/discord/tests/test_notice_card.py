"""The notice embed is red for a failure and neutral for information."""

from __future__ import annotations

from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.theme import COLOR_BLURPLE, COLOR_RED


def test_notice_embed_default_is_red() -> None:
    embed = build_notice_embed("Try again.", title="Stopped.")
    assert embed.color is not None and embed.color.value == COLOR_RED, "a failure is red"
    assert (embed.title, embed.description) == ("Stopped.", "Try again.")


def test_notice_embed_neutral_is_not_red() -> None:
    embed = build_notice_embed("Still working.", neutral=True)
    assert embed.color is not None and embed.color.value == COLOR_BLURPLE, (
        "information is neutral, not red"
    )
