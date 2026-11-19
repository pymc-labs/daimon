"""The buttons on a provider switch card posted before switches applied at once.

Discord routes a click to the application, not to the process that sent the
message, so the bot answers it: the card closes as expired.
"""

from __future__ import annotations

import re
from typing import Any, Self

from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.core.switch_cards import CUSTOM_ID_PATTERN, expired_switch_card

import discord


class SwitchCardControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=CUSTOM_ID_PATTERN,
):
    """Confirm or Cancel on one older switch card, rebuilt from its custom_id."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        card = expired_switch_card(button.custom_id or "")
        if card is None:
            raise ValueError("not a switch card control")
        self.card = card

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=getattr(item, "label", None),
                custom_id=match[0],
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=build_card_view(self.card), allowed_mentions=discord.AllowedMentions.none()
        )
