"""Screen-to-`discord.ui` renderer, and the REST post that opens a wizard run.

Posted from the MCP process (Cloud Run); every tap on the posted message is
dispatched later by the Discord bot process (worker VM). The two processes
cannot import each other (import-linter's independence contract), and the
layer they share -- `daimon.core.wizard` -- deliberately carries no Discord
SDK type, so each side owns a thin translation of the platform-neutral
`Screen` (`daimon.core.wizard.render`) into its own component tree. This
module is that translation for the posting side; a divergent copy here (a
button built in a different order, a different style mapping) would silently
desync the two renderers without either side raising, so the two are held to
producing byte-identical `to_components()` output for the same `Screen`.

A plain `discord.ui.View` cannot hold a `Container`, `TextDisplay`,
`Separator`, or `MediaGallery` -- those are components-v2 items, and
`View.add_item` raises `ValueError` for any of them unless the view is a
`discord.ui.LayoutView`. `LayoutView` is therefore the only view type built
here. The components-v2 message flag is never set by this module: the HTTP
layer sets it automatically from the view's own contents. A components-v2
message also cannot carry `content`, `embed`, `embeds`, `stickers`, or
`poll` -- which is why the screen's head text is rendered as a `TextDisplay`
component instead of message content.

Reuses `_credential_button.py`'s exact require/resolve/permission-check
order (itself borrowed from `_send.py`'s `_send_message_impl`) rather than
re-implementing any of it, so posting a wizard carries the same
channel-visibility discipline as every other message this adapter sends.

The invariant that makes cross-process re-rendering possible: the process
that redraws a later step runs elsewhere (the bot, on a tap) and cannot read
this process's file store, so whatever a later render needs has to be
resolvable from anywhere. That is what the content-delivery addresses this
module captures are for -- every step's image is therefore uploaded exactly
once, here, on the first post, even though only step one's image is
referenced by a component on this message.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._send import (
    _build_files_from_handles,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.wizard.render import Screen, ScreenButton, to_screen
from daimon.core.wizard.spec import WizardSpec
from daimon.core.wizard.state import WizardState, build_custom_id
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

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


def build_wizard_view(
    screen: Screen, *, files: Mapping[str, discord.File] | None = None
) -> discord.ui.LayoutView:
    """Translate a `Screen` into a real `LayoutView`.

    Component order is a contract: head text, a small separator, an optional
    media gallery, optional body text, an optional select row, then (after a
    large invisible separator gap) one action row per button row. A screen's
    image is resolved by preferring the matching `discord.File` in `files`
    (keyed by handle) over its persisted `url`, and is omitted entirely when
    neither resolves -- this is how a later step's already-uploaded image
    rides the first post's attachment set without being displayed on it.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=_ACCENT_COLOURS[screen.accent]
    )
    container.add_item(discord.ui.TextDisplay(screen.head_text))
    container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    if screen.image is not None:
        media: str | discord.File | None = None
        if files is not None and screen.image.handle is not None and screen.image.handle in files:
            media = files[screen.image.handle]
        elif screen.image.url is not None:
            media = screen.image.url
        if media is not None:
            gallery: discord.ui.MediaGallery[discord.ui.LayoutView] = discord.ui.MediaGallery()
            gallery.add_item(media=media)
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


@dataclass(frozen=True)
class PostedWizard:
    """What posting a wizard's first screen returns to its caller.

    `image_urls` maps every uploaded step's `image_handle` to the
    content-delivery address Discord minted for it -- the durable reference
    a later re-render (running in the other process, on a tap) resolves the
    image from, since it never sees this process's file store.
    """

    message_id: str
    image_urls: dict[str, str]


async def _post_wizard_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    spec: WizardSpec,
    short_id: str,
) -> PostedWizard:
    """Post a wizard's first screen, uploading every step's image.

    Only step one's image is referenced by a component; every other step's
    image still rides this same attachment set so its content-delivery
    address is captured now, before any tap, since a later render cannot
    reach this process's file store.
    """
    user_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    bot_token = _require_bot_token(runtime)

    handles = [step.image_handle for step in spec.steps if step.image_handle is not None]
    files: list[discord.File] = []
    if handles:
        files = await _build_files_from_handles(
            handles,
            session_factory=runtime.session_factory,
            tenant_id=auth.tenant_id,
        )
    files_by_handle = dict(zip(handles, files, strict=True))

    rendered_screen = to_screen(spec, WizardState(short_id=short_id))
    view = build_wizard_view(rendered_screen, files=files_by_handle)

    async with rest_client(bot_token) as c:
        _, member = await _resolve_member(c, guild_id, user_id)
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            # Thread.permissions_for needs the parent in the guild cache;
            # the per-call REST client starts with an empty one.
            await _ensure_thread_parent_cached(channel)
            await _check_thread_view(c, channel, member, user_id)
        _check_send_permission(channel, member)
        await _require_discord_channel_writable(runtime, auth, channel)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support sending messages")
        # The head text carries the agent-authored prompt and question
        # verbatim (only user-typed answers go through the summary's escape
        # pass), so an injected `@everyone` in a form would ping the channel.
        # Nothing in a form legitimately needs to mention anyone.
        sent = await channel.send(
            view=view, files=files, allowed_mentions=discord.AllowedMentions.none()
        )

    # Mapped by position, not by filename: a file's display name is slugged
    # from the agent-supplied image title, so two steps titled the same thing
    # upload under one filename and would both resolve to whichever
    # attachment came last. discord.py sends files in list order and Discord
    # returns attachments in that same order, so index i belongs to handle i.
    image_urls = {
        handle: attachment.url
        for handle, attachment in zip(handles, sent.attachments, strict=False)
    }
    missing = [handle for handle in handles if handle not in image_urls]
    if missing:
        logger.warning(
            "wizard post %s: no attachment url returned for image handles %r",
            sent.id,
            missing,
        )

    return PostedWizard(message_id=str(sent.id), image_urls=image_urls)
