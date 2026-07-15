"""`/routines` slash command — read-mostly status panel for guild routines.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.
"""

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.checks import (
    require_manage_guild,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.routines_panel.panel import RoutinesPanelView
from daimon.adapters.discord.routines_panel.read import load_guild_routines
from daimon.adapters.discord.routines_panel.state import RoutinesPanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class RoutinesCog(commands.Cog):
    """Admin-only panel for scheduled routines (hidden from non-manage_guild users, D-32)."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="routines",
        description="Show scheduled routines for this guild",
    )
    @require_registered_guild
    @require_manage_guild
    async def routines(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "guild_only ensures guild context"
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            assert tenant_id is not None, "require_registered_guild guarantees a tenant"
            async with runtime.sessionmaker() as session:
                entries, over_cap_count, agent_name_map = await load_guild_routines(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                )
            state = RoutinesPanelState.initial(
                rows=entries,
                over_cap_count=over_cap_count,
                agent_name_map=agent_name_map,
            )
            view = RoutinesPanelView(
                state,
                runtime=runtime,
                allowed_user_id=interaction.user.id,
            )
            await interaction.followup.send(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoutinesCog(bot))
