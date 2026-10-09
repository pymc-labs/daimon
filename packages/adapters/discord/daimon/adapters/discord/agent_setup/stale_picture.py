"""Refuse the Change button on setup panels rendered before picture uploads were turned off.

The old button kept its fixed custom_id, so a class-level `DynamicItem`
catches a click on it after a restart. It only answers with the refusal and
writes nothing.
"""

from __future__ import annotations

import re
from typing import Any, Self

from daimon.core.agent_identity import CUSTOM_PICTURES_OFF

import discord
from discord.ext import commands

STALE_CHANGE_ID = "agent-setup:avatar-change"


class StalePictureChangeButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=re.escape(STALE_CHANGE_ID),
):
    """The Change button on a setup panel rendered before uploads were turned off."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Change", custom_id=STALE_CHANGE_ID))

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls()

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        await interaction.response.send_message(CUSTOM_PICTURES_OFF, ephemeral=True)
