"""`/workflows`: pick a workflow to run, pause, resume or open.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.
"""

from typing import cast

from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.workflow_cards import workflows_panel
from daimon.core.platform_words import DESCRIPTIONS

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


@app_commands.guild_only()
class WorkflowsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(name="workflows", description=DESCRIPTIONS["workflows"])
    @require_registered_guild
    async def workflows(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type
        view, refusal = await workflows_panel(runtime.sessionmaker, runtime.settings, interaction)
        if view is None:
            await interaction.followup.send(embed=build_notice_embed(refusal or ""), ephemeral=True)
            return
        await interaction.followup.send(
            view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
