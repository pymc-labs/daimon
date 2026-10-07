"""Server-admin GitHub connection entry point."""

from __future__ import annotations

from typing import cast

from daimon.adapters.discord.checks import is_guild_admin, require_registered_guild
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import CONNECT_COPY, connect_link
from daimon.core.ma_identity import derive_tenant_uuid

import discord
from discord import app_commands
from discord.ext import commands


@app_commands.guild_only()
class GithubCog(commands.GroupCog, group_name="github", group_description="GitHub access"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="connect", description="Connect GitHub repos to this server")
    @require_registered_guild
    async def connect(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only a server admin can connect GitHub.", ephemeral=True
            )
            return
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with runtime.sessionmaker.begin() as session:
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=tenant_id,
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                )
        except ValueError:
            await interaction.followup.send(
                "GitHub connection is unavailable. Ask a server admin to check setup.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"{CONNECT_COPY}\n{url}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
