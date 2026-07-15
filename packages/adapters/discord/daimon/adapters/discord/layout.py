"""Shared Components V2 layout helpers — F5 design-language primitives.

Pure module: no I/O, no first-party imports, no classes.
Every V2 screen plan in this adapter imports these four helpers.
"""

from __future__ import annotations

import discord


def header(
    title: str, *, subtext: str | None = None
) -> discord.ui.TextDisplay[discord.ui.LayoutView]:
    """'## {title}' plus optional '\\n-# {subtext}' line. Pure."""
    content = f"## {title}"
    if subtext is not None:
        content = f"{content}\n-# {subtext}"
    return discord.ui.TextDisplay(content)


def hairline() -> discord.ui.Separator[discord.ui.LayoutView]:
    """Visible hairline divider, small spacing (discord.py defaults)."""
    return discord.ui.Separator()


def air_gap() -> discord.ui.Separator[discord.ui.LayoutView]:
    """Invisible large spacer: visible=False, spacing=discord.SeparatorSpacing.large."""
    return discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large)


def static_view(container: discord.ui.Container[discord.ui.LayoutView]) -> discord.ui.LayoutView:
    """LayoutView wrapping one container with no interactive children.

    Used for no-controls cards (/help, privacy post-delete) and the
    Done-button controls-less re-render pattern.
    """
    view = discord.ui.LayoutView()
    view.add_item(container)
    return view
