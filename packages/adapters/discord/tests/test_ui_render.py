"""A core `ui.Panel` drawn as one Components V2 container."""

from __future__ import annotations

from typing import Any

import discord
from daimon.adapters.discord import theme
from daimon.adapters.discord.ui_render import panel_container
from daimon.core import ui

_PANEL = ui.Panel(
    "Billing",
    (
        ui.titled("You", "$1.00 used this month"),
        ui.Section((ui.Figure("$9.00", "total credit left"), ui.Muted(("Ask an admin.",)))),
        ui.titled("Top spenders", "1. Ann  $1.00", more="+ 2 more"),
    ),
    subtitle=("May 2026", "$1.00 spent by 1 person"),
)


def _blocks(container: discord.ui.Container[Any]) -> list[str]:
    return [
        child.content if isinstance(child, discord.ui.TextDisplay) else "---"
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay | discord.ui.Separator)
    ]


def test_a_panel_is_a_heading_then_each_section_after_a_separator() -> None:
    assert _blocks(panel_container(_PANEL)) == [
        "## Billing\n-# May 2026\n-# $1.00 spent by 1 person",
        "---",
        "**You**\n$1.00 used this month",
        "---",
        "### $9.00\ntotal credit left\n-# Ask an admin.",
        "---",
        "**Top spenders**\n1. Ann  $1.00\n-# + 2 more",
    ], "titles bold, the figure a heading, grey rows small"


def test_the_tone_is_the_accent_and_controls_come_last() -> None:
    def accent(tone: ui.Tone | None) -> object:
        return panel_container(ui.Panel("T", (), tone=tone)).accent_colour

    assert [accent("alert"), accent("warning"), accent(None)] == [
        theme.COLOR_RED,
        theme.COLOR_AMBER,
        None,
    ]
    row = discord.ui.ActionRow[discord.ui.LayoutView](discord.ui.Button(label="Done"))
    container = panel_container(_PANEL, [row])
    assert container.children[-1] is row, "the controls end the panel"
    assert isinstance(container.children[-2], discord.ui.Separator)
