"""`/privacy` slash command — per-person data-rights panel (DM + guild).

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

No @app_commands.guild_only(); @app_commands.dm_permission(True) on the
command so /privacy is invocable from both DM and guild. No
@require_registered_guild — privacy is invoker-personal, available regardless
of guild registration state. Handler defers ephemeral before any DB read.
When find_platform_principal returns None, render the grey deleted-state
container and return.
"""

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord import layout
from daimon.adapters.discord.checks import resolve_tenant_for_interaction
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.privacy_panel.embeds import build_deleted_state_container
from daimon.adapters.discord.privacy_panel.panel import PrivacyPanelView
from daimon.adapters.discord.privacy_panel.read import (
    load_purge_preview,
    resolve_privacy_account,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


# NOTE: NO @app_commands.guild_only() — /privacy is per-person, available in DM + guild.
class PrivacyCog(commands.Cog):
    """Per-person data-rights panel (view / export / delete-me)."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="privacy",
        description="See, export, or delete what daimon stores about you",
    )
    # discord.py 2.4+ replaces dm_permission with allowed_contexts; explicitly
    # enable guilds + DMs + private channels so /privacy is invocable from both
    # DM and guild contexts.
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def privacy(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            user_name = interaction.user.name  # Discord username for typed-confirm
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            if tenant_id is None:
                # DM context or unprovisioned guild — no tenant, so no stored
                # data for this user under any tenant: render deleted-state.
                await interaction.followup.send(
                    view=layout.static_view(build_deleted_state_container(user_name)),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=True,
                )
                return
            async with runtime.sessionmaker() as session:
                account_id = await resolve_privacy_account(
                    session,
                    tenant_id=tenant_id,
                    platform_user_id=str(interaction.user.id),
                )
            if account_id is None:
                # No principal → render deleted-state and return.
                await interaction.followup.send(
                    view=layout.static_view(build_deleted_state_container(user_name)),
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=True,
                )
                return
            preview = await load_purge_preview(
                session_factory=runtime.sessionmaker,
                account_id=account_id,
            )
            view = PrivacyPanelView(
                runtime=runtime,
                account_id=account_id,
                allowed_user_id=interaction.user.id,
                user_name=user_name,
                preview=preview,
            )
            await interaction.followup.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            log.warning("privacy.handler.failed", rid=rid, error=str(exc))
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PrivacyCog(bot))
