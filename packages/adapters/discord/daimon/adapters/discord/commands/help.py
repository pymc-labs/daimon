"""HelpCog — flat /help slash command.

Ephemeral V2 Components card listing every day-1 slash currently registered on the
command tree plus the `@bot` configure-by-chat entrypoint. Open to all
guild members (no `manage_guild` requirement); guild registration is
still checked.

The slash list is a hand-edited module constant.

Note: `from __future__ import annotations` is intentionally omitted.
discord.py evaluates parameter annotations at import time to extract
slash command parameter metadata.
"""

from typing import cast

from daimon.adapters.discord import layout
from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.runtime import DiscordRuntime

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


def _body(bot_display_name: str) -> str:
    return f"""\
**Agent management**
-# /agent-setup — See your agents, who answers where, and make changes

**Routines**
-# /routines — Show scheduled routines for this guild

**Billing**
-# /billing — Show your billing usage (admins see per-member breakdown)

**Privacy**
-# /privacy — See, export, or delete what {bot_display_name} stores about you

**Meta**
-# /help — List commands and the @bot conversational entrypoint\
"""


def _conversational(bot_display_name: str) -> str:
    return f"""\
💬 **Or just talk to your agent**
-# @{bot_display_name} help me set up
-# @{bot_display_name} make a routine that runs daily\
"""


def build_help_view(bot_display_name: str = "daimon") -> discord.ui.LayoutView:
    """Build the static /help V2 LayoutView. Pure — no I/O."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header("📖 Commands", subtext="only you can see this"),
        layout.hairline(),
        discord.ui.TextDisplay(_body(bot_display_name)),
        layout.hairline(),
        discord.ui.TextDisplay(_conversational(bot_display_name)),
    )
    return layout.static_view(container)


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


@app_commands.guild_only()
class HelpCog(commands.Cog):
    """Flat /help slash command."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="help", description="List commands and the @bot conversational entrypoint"
    )
    @require_registered_guild
    async def help(self, interaction: BotInteraction) -> None:
        runtime = _get_runtime(interaction)
        bot_display_name = (
            runtime.settings.discord.bot_display_name
            if runtime.settings.discord is not None
            else "daimon"
        )
        await interaction.response.send_message(
            view=build_help_view(bot_display_name), ephemeral=True
        )
