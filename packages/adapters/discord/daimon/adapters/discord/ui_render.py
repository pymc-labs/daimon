"""A core `ui.Panel` as one Components V2 container.

The title is a `##` heading over `-#` subtitle rows; each section is one text
block after a separator; the tone is the accent colour.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from daimon.adapters.discord import theme
from daimon.core import ui

import discord

Container = discord.ui.Container[discord.ui.LayoutView]
Text = discord.ui.TextDisplay[discord.ui.LayoutView]

ACCENT: dict[ui.Tone | None, int | None] = {
    "alert": theme.COLOR_RED,
    "warning": theme.COLOR_AMBER,
    None: None,
}


def gap() -> discord.ui.Separator[discord.ui.LayoutView]:
    """The line and space between two blocks of a panel."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def section_text(section: ui.Section) -> str:
    lines = [f"**{section.title}**"] if section.title else []
    for block in section.blocks:
        if isinstance(block, ui.Figure):
            lines += [f"### {block.value}", *([block.caption] if block.caption else [])]
        elif isinstance(block, ui.Text):
            lines += block.lines
        else:
            lines += [f"-# {line}" for line in block.lines]
    return "\n".join(lines)


def panel_container(
    panel: ui.Panel, controls: Sequence[discord.ui.ActionRow[Any]] = ()
) -> Container:
    """The panel, then ``controls`` after a separator; a Done render passes none."""
    header = Text("\n".join([f"## {panel.title}", *(f"-# {line}" for line in panel.subtitle)]))
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [header]
    for section in panel.sections:
        children += [gap(), Text(section_text(section))]
    if controls:
        children += [gap(), *controls]
    return discord.ui.Container(*children, accent_colour=ACCENT[panel.tone])
