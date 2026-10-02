"""The ephemeral /here status card."""

from __future__ import annotations

from typing import cast

from daimon.adapters.discord import layout
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.errors import DaimonError
from daimon.core.here_card import load_here_card
from daimon.core.stores.identity import find_platform_principal

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


def build_here_view(card_text: str) -> discord.ui.LayoutView:
    """Render long cards within Discord's per-text-display size limit."""
    displays: list[discord.ui.TextDisplay[discord.ui.LayoutView]] = [
        discord.ui.TextDisplay(card_text[i : i + 3500]) for i in range(0, len(card_text), 3500)
    ]
    return layout.static_view(discord.ui.Container(*displays))


@app_commands.guild_only()
class HereCog(commands.Cog):
    """Report the current agent and its reach from code, not a model answer."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="here", description="Who answers here, what it can read and holds")
    @require_registered_guild
    async def here(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            if tenant_id is None or interaction.guild is None or interaction.channel is None:
                raise DaimonError("Run /here in a server channel.")
            channel = interaction.channel
            thread_id: str | None = None
            if isinstance(channel, discord.Thread):
                thread_id = str(channel.id)
                channel = channel.parent
            if not isinstance(channel, discord.abc.GuildChannel):
                raise DaimonError("Run /here in a server channel.")
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            bot_member = interaction.guild.me
            visible_ids = {
                str(item.id)
                for item in interaction.guild.channels
                if member is not None
                and item.permissions_for(member).view_channel
                and item.permissions_for(bot_member).view_channel
            }
            bot_view: bool | None = None
            try:
                await interaction.guild.fetch_channel(channel.id)
                bot_view = True
            except discord.Forbidden as exc:
                if exc.code == 50001:
                    bot_view = False
                else:
                    raise
            category_visible: list[tuple[str, str]] = []
            if channel.category_id is not None:
                for sibling in interaction.guild.channels:
                    if sibling.id == channel.id or sibling.category_id != channel.category_id:
                        continue
                    if member is None or not sibling.permissions_for(member).view_channel:
                        continue
                    if not sibling.permissions_for(bot_member).view_channel:
                        category_visible.append((str(sibling.id), f"#{sibling.name}: no"))
                        continue
                    try:
                        await interaction.guild.fetch_channel(sibling.id)
                        category_visible.append((str(sibling.id), f"#{sibling.name}: yes"))
                    except discord.Forbidden as exc:
                        if exc.code != 50001:
                            raise
                        category_visible.append((str(sibling.id), f"#{sibling.name}: no"))
            github = runtime.settings.github
            async with runtime.sessionmaker() as session:
                principal = await find_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                card = await load_here_card(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    platform="discord",
                    channel_id=str(channel.id),
                    thread_id=thread_id,
                    default=runtime.deployment_default,
                    github=GitHubDeploymentFacts(
                        has_fallback_pat=github.fallback_pat is not None,
                        app_configured=github.app_id is not None
                        and github.app_private_key is not None,
                    ),
                    public_mcp_url=str(runtime.settings.mcp.public_url)
                    if runtime.settings.mcp.public_url is not None
                    else None,
                    is_admin=is_guild_admin(interaction),
                    caller_account_id=principal.account_id if principal else None,
                    visible_channel_ids=visible_ids,
                    bot_can_view=bot_view,
                    caller_can_view=member is not None
                    and channel.permissions_for(member).view_channel,
                    category_channels_bot_can_view=category_visible,
                )
            await interaction.edit_original_response(
                view=build_here_view(card.text),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, discord.HTTPException) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                allowed_mentions=discord.AllowedMentions.none(),
            )
