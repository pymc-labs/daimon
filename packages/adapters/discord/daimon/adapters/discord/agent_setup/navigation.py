"""The chrome every setup-panel screen shares: gating, swapping, Done, paging.

The three read-only screens are one ephemeral message that changes shape, not
three messages, so every navigation is an edit of the message the panel already
owns. ``PanelViewBase`` owns that edit, the invoker gate and the Done button so
the screens differ only in what they render.

Nothing here carries a ``custom_id``: every click lands in this process, on a
live view instance, within the panel's ten-minute life.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, Self

from daimon.adapters.discord.agent_setup.expiry import ExpiringView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.roster import Page

import discord

INVOKER_ONLY_MESSAGE: Final = "Only the command invoker can use these buttons."
STALE_PANEL_MESSAGE: Final = "This panel has moved on. Use the controls currently shown."

PANEL_TIMEOUT_SECONDS: Final = 600


class PanelViewBase(ExpiringView, discord.ui.LayoutView):
    """Base for the roster, details and routing screens of one setup panel.

    Every subclass builds its own container in ``__init__`` and attaches its own
    callbacks; this class only supplies what all three do identically.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # discord.py's base is typed against the broader Interaction[Client]
        """Refuse everyone but the person who ran the command.

        The panel is ephemeral, so nobody else should see it at all; this is the
        guard for the case where they somehow do.
        """
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(INVOKER_ONLY_MESSAGE, ephemeral=True)
            return False
        if self._is_superseded():
            await interaction.response.send_message(STALE_PANEL_MESSAGE, ephemeral=True)
            return False
        return True

    async def swap_to(self, interaction: discord.Interaction, view: PanelViewBase) -> None:
        """Replace what the panel's one message shows with ``view``.

        ``interaction.response.edit_message`` is the path for a click that has
        not been acknowledged yet; a callback that had to defer first (because
        it reads MA or the database before it knows what to render) has already
        spent the response, and ``edit_original_response`` reaches the same
        message afterwards.
        """
        if self._is_superseded():
            if interaction.response.is_done():
                await interaction.followup.send(STALE_PANEL_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(STALE_PANEL_MESSAGE, ephemeral=True)
            return
        if interaction.response.is_done():
            await interaction.edit_original_response(
                view=view.bind_render_interaction(interaction, panel=self.state),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            view=view.bind_render_interaction(interaction, panel=self.state),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_done(self, interaction: discord.Interaction) -> None:
        """Take the panel off screen and stop this view's timer."""
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()

    def done_button(self) -> discord.ui.Button[Self]:
        """The Done button, identical on every screen."""
        button: discord.ui.Button[Self] = discord.ui.Button(
            label="Done", style=discord.ButtonStyle.secondary
        )
        button.callback = self._on_done  # type: ignore[method-assign]  # per-instance callback; the class defines no button decorator
        return button

    def page_row[T](
        self,
        page: Page[T],
        *,
        on_previous: Callable[[discord.Interaction], Awaitable[None]],
        on_next: Callable[[discord.Interaction], Awaitable[None]],
    ) -> discord.ui.ActionRow[Self] | None:
        """The pager, or None when everything already fits on one page.

        Returning None rather than a row of two disabled buttons keeps three
        components out of a view that has no use for them.
        """
        if page.page_count <= 1:
            return None
        row: discord.ui.ActionRow[Self] = discord.ui.ActionRow()
        previous: discord.ui.Button[Self] = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=not page.has_previous,
        )
        previous.callback = on_previous  # type: ignore[method-assign]  # per-instance callback
        following: discord.ui.Button[Self] = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=not page.has_next,
        )
        following.callback = on_next  # type: ignore[method-assign]  # per-instance callback
        row.add_item(previous)
        row.add_item(following)
        return row
