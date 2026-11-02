"""The embed Daimon posts for a system notice, so it never reads as the agent's words."""

from __future__ import annotations

from daimon.adapters.discord.theme import COLOR_BLURPLE, COLOR_RED

import discord

# Discord caps an embed description at 4,096 characters.
_DESCRIPTION_MAX_CHARS = 4096


def build_notice_embed(
    text: str, *, title: str | None = None, neutral: bool = False
) -> discord.Embed:
    """One system notice, sent on its own: red for a failure, `neutral` for information."""
    return discord.Embed(
        color=COLOR_BLURPLE if neutral else COLOR_RED,
        title=title,
        description=text[:_DESCRIPTION_MAX_CHARS],
    )
