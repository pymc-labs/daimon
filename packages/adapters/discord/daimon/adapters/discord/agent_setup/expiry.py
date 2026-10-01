"""Shared expiry handling for every /agent-setup view.

discord.py never populates ``view.message`` for a view sent through an
interaction response (``_ViewStore.add_view`` only registers a component
dispatcher) — so the *interaction* that produced a view's message, not a
``discord.Message`` reference, is the only handle left once the view's own
buttons/selects have gone quiet. ``interaction.edit_original_response()`` is
the one call that can still reach that message afterward, on any interaction
object, regardless of whether it was originally a slash command, a component
click, or a modal submit.

``ExpiringView`` is a mixin the five /agent-setup views co-inherit so they
share one ``on_timeout`` instead of each hand-rolling the disable/notify
dance. Render sites call ``bind_render_interaction`` at render time (never at
``__init__``) so the mixin always holds the interaction whose original
response is the message currently on screen.
"""

from __future__ import annotations

from typing import Self

from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.layout import header, static_view
from daimon.adapters.discord.theme import COLOR_GREYPLE

import discord

EXPIRED_PANEL_TITLE = "⏳ Panel expired"
EXPIRED_PANEL_SUBTEXT = "Run `/agent-setup` again to continue."
EXPIRED_BUTTON_LABEL = "Expired — run /agent-setup"


def build_expired_view() -> discord.ui.LayoutView:
    """Controls-less terminal render for an expired /agent-setup view.

    Carries no interactive children at all — nothing needs disabling, and
    discord.py registers no new dispatcher or timer for it.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        header(EXPIRED_PANEL_TITLE, subtext=EXPIRED_PANEL_SUBTEXT),
        accent_colour=COLOR_GREYPLE,
    )
    return static_view(container)


async def edit_expired_message(
    view: discord.ui.View | discord.ui.LayoutView,
    *,
    interaction: discord.Interaction | None,
) -> None:
    """Edit the interaction's original response to ``view``.

    Returns immediately when ``interaction is None`` — a view that was never
    rendered has no message to expire. ``discord.NotFound`` is the ONE
    expected failure (the token or message is already gone, so there is
    nothing to notify) and the only exception this may catch: ``on_timeout``
    is dispatched as a bare asyncio task with no library-side handler, which
    makes this the named background boundary for error-handling purposes. Do
    not widen this catch, log-and-swallow, or retry.
    """
    if interaction is None:
        return
    try:
        await interaction.edit_original_response(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.NotFound:
        return


class ExpiringView:
    """Mixin supplying the one shared ``on_timeout`` every /agent-setup view needs.

    Holds no discord API state and defines no ``__init__`` — co-inheriting
    this mixin leaves every view's own ``super().__init__(timeout=...)``
    reaching ``discord.ui.View`` / ``discord.ui.LayoutView`` untouched, so
    timeout values are never affected by adding this mixin.
    """

    _render_interaction: discord.Interaction | None = None
    _render_panel: PanelState | None = None
    _render_seq: int = 0

    def bind_render_interaction(
        self, interaction: discord.Interaction, *, panel: PanelState | None
    ) -> Self:
        """Record the interaction and render generation for THIS render.

        Call this at the RENDER site, not in ``__init__`` — the binding must
        name the interaction whose original response is the message this
        render actually produced, and it must be refreshed whenever the same
        instance is re-rendered (see ``CredentialsSubView._reload_and_rerender``).
        """
        self._render_interaction = interaction
        self._render_panel = panel
        if panel is not None:
            panel.render_seq += 1
            self._render_seq = panel.render_seq
        return self

    def _is_superseded(self) -> bool:
        """True when a newer view has since been rendered onto the same panel.

        ``_ViewStore.add_view`` neither stops nor cancels the view it
        replaces, so a view that is no longer on screen still owns a live
        timer; this generation check is what stops it from overwriting the
        message a newer view owns.
        """
        return self._render_panel is not None and self._render_panel.render_seq > self._render_seq

    async def on_timeout(self) -> None:
        """Shared on_timeout implementation: expire unless superseded."""
        if self._is_superseded():
            return
        if self._render_panel is not None:
            # Expiry is itself a terminal render. Fence callbacks and modal
            # submissions that started from this panel before the timeout.
            self._render_panel.render_seq += 1
        await edit_expired_message(build_expired_view(), interaction=self._render_interaction)
