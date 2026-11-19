"""Answer Switch on a conversation model card posted before picks applied at once.

The old card's custom_id held a revision and a digest; a class-level `DynamicItem` catches
a click after a restart. The conversation already runs its picked model, so the click only
says so and writes nothing.
"""

from __future__ import annotations

import re
from typing import Any, Final, Self

from daimon.adapters.discord.notice_card import build_notice_embed

import discord
from discord.ext import commands

STALE_TEMPLATE: Final = r"tmr:[0-9]{1,18}:[0-9a-f]{64}"
ALREADY_RUNS: Final = (
    "This conversation already runs on the model picked for it.\nUse /model to change it."
)


class StaleThreadSwitchButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=STALE_TEMPLATE
):
    """Switch on an old conversation model card; registered in `setup_hook`."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(label="Switch", custom_id=custom_id))

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(match.group(0))

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        await interaction.response.send_message(
            embed=build_notice_embed(ALREADY_RUNS, neutral=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
