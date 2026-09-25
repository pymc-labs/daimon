"""Interactive View components for Discord UI.

GuardedView gates on allowed_user_id and prevents double-click via _handled flag.
CancelView for interrupting running turns (no timeout -- lifecycle manages removal).

All Views are non-persistent (VIEW-04): no custom_id, no bot.add_view(). CancelView uses
timeout=None -- lifecycle manages removal.

The documented exceptions: the chat-initiated credential-request button
(`daimon.adapters.discord.credential_button.CredentialRequestButton`), the
wizard's navigation button and multi-select
(`daimon.adapters.discord.wizard.WizardNavButton`,
`daimon.adapters.discord.wizard.WizardSelect`), and the private-feedback
button (`daimon.adapters.discord.feedback_button.FeedbackButton`) ARE
persistent -- each carries a custom_id and is registered as a
`discord.ui.DynamicItem` class in `setup_hook`. The credential button and the
wizard's first screen are posted by the MCP server, a different process from
the one that dispatches their click (this bot) -- there is no live in-memory
View instance to attach a non-persistent handler to. `FeedbackButton` is
persistent for a related but distinct reason: it is delivered into a direct
message and must still dispatch after this bot process restarts, so there is
again no live View instance to rely on. Every other interactive component in
this adapter stays non-persistent.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from daimon.adapters.discord.turn_card_recovery import turn_card_custom_id

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

    def __init__(
        self,
        *,
        allowed_user_id: int,
        cancel: asyncio.Event,
        turn_id: UUID | None = None,
    ) -> None:
        super().__init__(allowed_user_id=allowed_user_id, timeout=None)
        self._cancel = cancel
        if turn_id is not None:
            self.cancel_button.custom_id = turn_card_custom_id(turn_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[CancelView],
    ) -> None:
        self._cancel.set()
        self._finalize()
        await interaction.response.edit_message(view=self)
