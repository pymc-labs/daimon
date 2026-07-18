"""Interactive View components for Discord UI.

GuardedView gates on allowed_user_id and prevents double-click via _handled flag.
CancelView for interrupting running turns (no timeout -- lifecycle manages removal).

All Views are non-persistent (VIEW-04): no custom_id, no bot.add_view(). CancelView uses
timeout=None -- lifecycle manages removal.
"""

from __future__ import annotations

import asyncio

import discord


class GuardedView(discord.ui.View):
    """Base View with user-gating and single-handler semantics."""

    def __init__(self, *, allowed_user_id: int, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self.allowed_user_id = allowed_user_id
        self._handled = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # discord.py base uses broader type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        if self._handled:
            await interaction.response.send_message(
                "Already handled.",
                ephemeral=True,
            )
            return False
        return True

    def _finalize(self) -> None:
        self._handled = True
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]  # Item union; buttons have .disabled
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]  # Item union; buttons have .disabled
        if self.message is not None:
            await self.message.edit(view=self)


class CancelView(GuardedView):
    """Turn cancel button (no timeout -- lifecycle manages removal)."""

    def __init__(self, *, allowed_user_id: int, cancel: asyncio.Event) -> None:
        super().__init__(allowed_user_id=allowed_user_id, timeout=None)
        self._cancel = cancel

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[CancelView],
    ) -> None:
        self._cancel.set()
        self._finalize()
        await interaction.response.edit_message(view=self)
