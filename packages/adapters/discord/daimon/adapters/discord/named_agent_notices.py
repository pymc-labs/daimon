"""One Components V2 layout for named-agent refusal notices."""

from __future__ import annotations

from daimon.adapters.discord import layout, theme
from daimon.adapters.discord.thread_handoff import build_custom_id
from daimon.core.turn.errors import NamedAgentRefused

import discord


def build_named_agent_notice(err: NamedAgentRefused) -> discord.ui.LayoutView:
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=theme.COLOR_RED
    )
    container.add_item(layout.header(err.title))
    container.add_item(layout.hairline())
    container.add_item(discord.ui.TextDisplay(err.detail))
    if err.hand_over_agent_id is not None and err.hand_over_agent_name is not None:
        container.add_item(discord.ui.Separator(visible=False))
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        row.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=f"Switch to {err.hand_over_agent_name}"[:80],
                custom_id=build_custom_id(err.hand_over_agent_id),
            )
        )
        container.add_item(row)
    return layout.static_view(container)
