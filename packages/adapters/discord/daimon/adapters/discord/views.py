"""Interactive View components for Discord UI.

GuardedView gates on allowed_user_id and prevents double-click via _handled flag.
CancelView for interrupting running turns (no timeout -- lifecycle manages removal);
a current server admin may also press its Stop.

All Views are non-persistent: no custom_id, no bot.add_view(). CancelView uses
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
again no live View instance to rely on. Opt-in fleet controls likewise use the
registered `posted_controls.fleet.FleetControl` item to recover after restart.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from daimon.adapters.discord.checks import ADMIN_NOUN, admin_caller
from daimon.adapters.discord.turn_card_recovery import turn_card_custom_id

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.lifecycle import EditFn

log = structlog.get_logger()

STOP_NOT_ALLOWED = f"Only the person who asked, or {ADMIN_NOUN}, can stop this reply."


async def finish_control(
    interaction: discord.Interaction,
    *,
    view: discord.ui.View | discord.ui.LayoutView,
    allowed_mentions: discord.AllowedMentions | None = None,
    edit: EditFn | None = None,
) -> None:
    """A completed Stop or Resume uses the existing missing-card boundary."""
    try:
        if edit is not None:
            if interaction.message is None:
                return
            await interaction.response.defer()
            await edit(interaction.message, view=view, allowed_mentions=allowed_mentions)
        elif allowed_mentions is None:
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.edit_message(view=view, allowed_mentions=allowed_mentions)
    except discord.HTTPException as err:
        if err.code != 10008:
            raise
        log.info("turn.cancel_message_missing")


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
        self._turn_id = turn_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # discord.py base uses broader type
        """The asker, or whoever is a server admin at the moment they press Stop."""
        if interaction.user.id == self.allowed_user_id:
            return await super().interaction_check(interaction)
        if not admin_caller(interaction.user).is_server_admin:
            await interaction.response.send_message(STOP_NOT_ALLOWED, ephemeral=True)
            return False
        if self._handled:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.grey)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[CancelView],
    ) -> None:
        log.info(
            "turn.stop_pressed",
            platform="discord",
            turn_id=str(self._turn_id) if self._turn_id is not None else None,
            requester_id=str(self.allowed_user_id),
            pressed_by=str(interaction.user.id),
            by_admin=interaction.user.id != self.allowed_user_id,
        )
        self._cancel.set()
        self._finalize()
        await finish_control(interaction, view=self)
