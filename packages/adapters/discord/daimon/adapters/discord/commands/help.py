"""HelpCog — flat /help slash command.

Ephemeral V2 Components card listing every day-1 slash currently registered on the
command tree plus the `@bot` configure-by-chat entrypoint. Open to all
guild members (no `manage_guild` requirement); guild registration is
still checked.

Per Phase 30 D-LIST-01, the slash list is a hand-edited module constant.

Note: `from __future__ import annotations` is intentionally omitted.
discord.py evaluates parameter annotations at import time to extract
slash command parameter metadata.
"""

from daimon.adapters.discord import layout
from daimon.adapters.discord.checks import require_registered_guild

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_BODY = """\
**Agent management**
-# /agent-setup — Manage this server's agents

**Routines**
-# /routines — Show scheduled routines for this guild

**Billing**
-# /billing — Show your billing usage (admins see per-member breakdown)

**Privacy**
-# /privacy — See, export, or delete what daimon stores about you

**Meta**
-# /help — List commands and the @bot conversational entrypoint\
"""

_CONVERSATIONAL = """\
💬 **Or just talk to your agent**
-# @daimon help me set up
-# @daimon make a routine that runs daily\
"""


def build_help_view() -> discord.ui.LayoutView:
    """Build the static /help V2 LayoutView. Pure — no I/O, zero args."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header("📖 Commands", subtext="only you can see this"),
        layout.hairline(),
        discord.ui.TextDisplay(_BODY),
        layout.hairline(),
        discord.ui.TextDisplay(_CONVERSATIONAL),
    )
    return layout.static_view(container)


@app_commands.guild_only()
class HelpCog(commands.Cog):
    """Flat /help slash command (Phase 30 D-SHAPE-01)."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="help", description="List commands and the @bot conversational entrypoint"
    )
    @require_registered_guild
    async def help(self, interaction: BotInteraction) -> None:
        await interaction.response.send_message(view=build_help_view(), ephemeral=True)
