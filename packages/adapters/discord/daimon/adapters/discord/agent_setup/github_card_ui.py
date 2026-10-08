"""Embeds for private GitHub status and request cards."""

from __future__ import annotations

from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_BLURPLE, COLOR_GREEN, COLOR_RED

import discord


def github_embed(text: str, *, state: str = "info") -> discord.Embed:
    title, _, detail = text.partition("\n")
    color = {
        "waiting": COLOR_AMBER,
        "success": COLOR_GREEN,
        "danger": COLOR_RED,
    }.get(state, COLOR_BLURPLE)
    embed = discord.Embed(title=title[:256], color=color)
    if detail:
        if detail.startswith("Can: "):
            embed.add_field(name="Can", value=detail.removeprefix("Can: ")[:1024], inline=False)
        else:
            embed.add_field(name="Details", value=detail[:1024], inline=False)
    embed.set_footer(text="GitHub on Daimon")
    return embed
