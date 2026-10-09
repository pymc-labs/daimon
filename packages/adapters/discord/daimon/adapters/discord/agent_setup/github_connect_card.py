"""Discord embed for a private or requester-bound GitHub connection card."""

from __future__ import annotations

from daimon.core.github_connect_cards import ConnectCard, discord_embed_payload

import discord


def connect_embed(card: ConnectCard) -> discord.Embed:
    payload = discord_embed_payload(card)
    payload["description"] = discord.utils.escape_markdown(payload["description"])
    return discord.Embed.from_dict(payload)
