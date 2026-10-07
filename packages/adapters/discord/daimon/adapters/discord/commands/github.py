"""Private `/github` entry point for Discord server admins."""

from __future__ import annotations

from typing import cast

from daimon.adapters.discord.agent_setup.github_home import load_home
from daimon.adapters.discord.agent_setup.hydrate import load_roster_state
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.runtime import DiscordRuntime

import discord
from discord import app_commands
from discord.ext import commands

BotInteraction = discord.Interaction[commands.Bot]


@app_commands.guild_only()
class GithubCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="github", description="Manage GitHub repos for agents")
    @require_registered_guild
    async def github(self, interaction: BotInteraction) -> None:
        if interaction.guild_id is None or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Server admins connect repos here.", ephemeral=True
            )
            return
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        await interaction.response.defer(ephemeral=True, thinking=True)
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant_id is None:
            await interaction.followup.send("GitHub is unavailable here.", ephemeral=True)
            return
        state = await load_roster_state(runtime, interaction, tenant_id=tenant_id, is_admin=True)
        home = await load_home(state, runtime=runtime, user_id=interaction.user.id)
        await interaction.edit_original_response(
            view=home.bind_render_interaction(interaction, panel=state),
            allowed_mentions=discord.AllowedMentions.none(),
        )
