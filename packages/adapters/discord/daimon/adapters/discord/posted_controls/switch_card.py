"""The buttons on Daimon's own provider switch card.

The MCP process posts the card for a turn whose provider shows none (OpenAI,
Gemini); its buttons dispatch here, in the bot process, because Discord routes
a click to the application, not to whichever process sent the message.
`daimon.core.switch_cards.press_switch_card` decides everything: only the
requester's press counts, a card is claimed once, and Confirm re-verifies the
switch exactly as the Claude card's click does before applying it.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

import structlog
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.switch_cards import (
    CUSTOM_ID_PATTERN,
    SwitchAction,
    answered_switch_card,
    press_switch_card,
)

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot

log = structlog.get_logger()

#: A press by anyone but the requester: the card stays as it is.
NOT_YOURS = "Only the person who asked can confirm this switch."
UNAVAILABLE = "This switch card is unavailable. Ask again to redo it."


class SwitchCardControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=CUSTOM_ID_PATTERN,
):
    """Confirm or Cancel on one switch card, rebuilt from its custom_id after restarts."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        match = self.template.fullmatch(button.custom_id or "")
        if match is None:
            raise ValueError("not a switch card control")
        self.card_id = uuid.UUID(match["card"])
        self.press: SwitchAction = cast(SwitchAction, match["action"])

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=getattr(item, "label", None),
                custom_id=match[0],
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.bot import requester_role
        from daimon.core.stores.domain import Role

        bot = cast("DaimonBot", interaction.client)
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(
                embed=build_notice_embed(UNAVAILABLE), ephemeral=True
            )
            return
        # Re-verifying reads the agent's setup from its provider: past the 3 s budget.
        await interaction.response.defer()

        async def current_admin(clicker: str) -> bool:
            role, _ = await requester_role(guild, clicker)
            return role is Role.ADMIN

        runtime = bot.runtime
        try:
            press = await press_switch_card(
                deps=runtime.turn_deps,
                sessionmaker=runtime.sessionmaker,
                anthropic=runtime.anthropic,
                turn_settings=runtime.settings.turn,
                deployment_default=runtime.deployment_default,
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild.id)),
                card_id=self.card_id,
                action=self.press,
                clicker=str(interaction.user.id),
                current_admin=current_admin,
                place_word="server",
                now=datetime.now(UTC),
                message_id=str(interaction.message.id),
            )
        except Exception:
            log.exception("switch_card.press_failed", card_id=str(self.card_id))
            await interaction.followup.send(embed=build_notice_embed(UNAVAILABLE), ephemeral=True)
            return
        if press is None:
            await interaction.followup.send(embed=build_notice_embed(NOT_YOURS), ephemeral=True)
            return
        if not press.closed:
            await interaction.followup.send(
                embed=build_notice_embed(press.text, neutral=True), ephemeral=True
            )
            return
        await interaction.message.edit(
            view=build_card_view(answered_switch_card(press)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
