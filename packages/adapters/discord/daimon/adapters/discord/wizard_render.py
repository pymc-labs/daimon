"""Screen-to-`discord.ui` renderer for the bot process.

This is the SECOND copy of the Screen-to-`LayoutView` translation --
`daimon.adapters.mcp.tools.discord._wizard`'s `build_wizard_view` is the
first, run by the process that posts a wizard's first screen. The two
processes cannot import each other (import-linter's independence contract),
and the layer they share -- `daimon.core.wizard.render` -- deliberately
carries no Discord SDK type, so each side owns a thin, from-scratch
translation of the platform-neutral `Screen` into its own component tree.
A change to component order, style mapping, or spacing in one renderer
without the matching change in the other would silently desync what a user
sees after a tap from what they saw on the original post, so any change
here must be mirrored there.

The one deliberate divergence: this process cannot attach files -- there is
no local `discord.File` handle to re-upload on a re-render, since the bot
process never touched the MCP process's file store. A `Screen.image` is
therefore resolved from `screen.image.url` only (the persisted
content-delivery address `_post_wizard_impl` captured on first post); when
only a `handle` is present and no `url`, the image is simply omitted rather
than raising -- that combination should not occur for a step already posted
(the posting side always resolves and stores a url for every image it
uploads), but a re-render must degrade gracefully rather than crash a tap.

Component order is a contract: head text, a small separator, an optional
media gallery, optional body text, an optional select row, then (after a
large invisible separator gap) one action row per button row.
"""

from __future__ import annotations

from daimon.core.wizard.render import Screen, ScreenButton
from daimon.core.wizard.state import build_custom_id

import discord

_ACCENT_COLOURS: dict[str, int] = {
    "blurple": 0x5865F2,
    "green": 0x57F287,
}

_BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
}


def _build_button(short_id: str, button: ScreenButton) -> discord.ui.Button[discord.ui.LayoutView]:
    return discord.ui.Button(
        style=_BUTTON_STYLES[button.style],
        label=button.label,
        custom_id=build_custom_id(short_id, button.action),
        disabled=button.disabled,
    )


def build_wizard_view(screen: Screen) -> discord.ui.LayoutView:
    """Translate a `Screen` into a real `LayoutView`, matching the posting
    process's component order exactly. See the module docstring for the one
    deliberate divergence (image resolution from `url` only, no `files`)."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=_ACCENT_COLOURS[screen.accent]
    )
    container.add_item(discord.ui.TextDisplay(screen.head_text))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    if screen.image is not None and screen.image.url is not None:
        gallery: discord.ui.MediaGallery[discord.ui.LayoutView] = discord.ui.MediaGallery()
        gallery.add_item(media=screen.image.url)
        container.add_item(gallery)

    if screen.body_text is not None:
        container.add_item(discord.ui.TextDisplay(screen.body_text))

    if screen.select is not None:
        select: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
            custom_id=build_custom_id(screen.short_id, screen.select.action),
            placeholder=screen.select.placeholder,
            min_values=screen.select.min_values,
            max_values=screen.select.max_values,
            options=[
                discord.SelectOption(
                    label=option.label,
                    value=option.value,
                    description=option.description,
                    default=option.default,
                )
                for option in screen.select.options
            ],
        )
        select_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        select_row.add_item(select)
        container.add_item(select_row)

    if screen.button_rows:
        container.add_item(
            discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large)
        )
        for row in screen.button_rows:
            action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            for button in row:
                action_row.add_item(_build_button(screen.short_id, button))
            container.add_item(action_row)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view
