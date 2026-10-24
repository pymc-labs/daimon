"""HelpCog — flat /help slash command.

Ephemeral V2 Components card listing every day-1 slash currently registered on the
command tree plus the `@bot` configure-by-chat entrypoint. Open to all
guild members (no `manage_guild` requirement); guild registration is
still checked.

The slash list is built from `platform_words`, plus `/github home`.

Note: `from __future__ import annotations` is intentionally omitted.
discord.py evaluates parameter annotations at import time to extract
slash command parameter metadata.
"""

from typing import cast

from daimon.adapters.discord import layout
from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.platform_words import DESCRIPTIONS, WORDS

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


# Not an action in the shared table: the second command of `/github`.
_GITHUB_HOME = ("/github home", "Your repos, GitHub account and waiting requests")


def _command_rows() -> list[tuple[str, str]]:
    """`(command, description)` for every Discord command, in the table's order."""
    rows: list[tuple[str, str]] = []
    for action, command in WORDS["discord"].commands.items():
        rows.append((command.word, DESCRIPTIONS[action]))
        if action == "github":
            rows.append(_GITHUB_HOME)
    return rows


def _body() -> str:
    rows = _command_rows()
    width = max(len(word) for word, _ in rows) + 2
    return "\n".join(f"{word.ljust(width)}{description}" for word, description in rows)


def _conversational(bot_display_name: str) -> str:
    return f"""\
💬 Or just ask

@{bot_display_name} help me set up\
"""


def build_help_view(bot_display_name: str = "daimon") -> discord.ui.LayoutView:
    """Build the static /help V2 LayoutView. Pure — no I/O."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        discord.ui.TextDisplay("## 📖 Commands\n\n-# Only you can see this."),
        layout.hairline(),
        discord.ui.TextDisplay(_body()),
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

    @app_commands.command(name="help", description=DESCRIPTIONS["help"])
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
