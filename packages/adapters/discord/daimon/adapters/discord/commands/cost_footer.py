"""`/cost-footer` — show or hide the line under each finished answer, server-wide.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

The line names the agent, the time taken, the cost and the money left. With
no choice the command says whether it is shown; only an admin may change it.
Asking in words reaches the same setting through the agent's
`set_cost_footer` tool.
"""

from typing import cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.cost_footer import cost_footer_note
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores.tenants import get_cost_footer, set_cost_footer
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_NOT_REGISTERED = "Daimon isn't set up in this server yet."
_NOT_ALLOWED = "Only a server admin can show or hide the cost footer."


@app_commands.guild_only()
class CostFooterCog(commands.Cog):
    """Server-wide switch for the finished-turn footer."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="cost-footer", description=DESCRIPTIONS["cost_footer"])
    @app_commands.describe(choice="Leave empty to see whether it is shown")
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="Show", value="show"),
            app_commands.Choice(name="Hide", value="hide"),
        ]
    )
    @require_registered_guild
    async def cost_footer(
        self, interaction: BotInteraction, choice: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            if tenant_id is None:
                raise UserFacingError(_NOT_REGISTERED)
            if choice is None:
                async with runtime.sessionmaker() as session:
                    shown = await get_cost_footer(session, tenant_id=tenant_id)
            else:
                if not await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS):
                    raise UserFacingError(_NOT_ALLOWED)
                shown = choice.value == "show"
                async with runtime.sessionmaker.begin() as session:
                    await set_cost_footer(session, tenant_id=tenant_id, shown=shown)
            await interaction.edit_original_response(
                content=cost_footer_note(shown), allowed_mentions=discord.AllowedMentions.none()
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                allowed_mentions=discord.AllowedMentions.none(),
            )
