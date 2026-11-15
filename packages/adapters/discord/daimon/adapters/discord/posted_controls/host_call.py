"""The Approve and Deny buttons on Daimon's own card for a gated tool call.

The MCP gate posts the card when the turn's provider shows none (OpenAI, Gemini);
the click lands here because Discord routes it to the application. A press only
answers the card: `daimon.core.host_calls.press_host_call` claims it once and
queues the wake turn that runs the approved call.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

import structlog
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.core.features.snapshot import deployment_on
from daimon.core.host_calls import (
    CUSTOM_ID_PATTERN,
    CallAction,
    answered_call_card,
    press_host_call,
)
from daimon.core.ma_identity import derive_tenant_uuid

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot

log = structlog.get_logger()

NOT_YOURS = "Only the person who asked can answer this."
UNAVAILABLE = "This card is unavailable. Ask again to redo it."


class HostCallControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=CUSTOM_ID_PATTERN,
):
    """Approve or Deny on one call card, rebuilt from its custom_id after restarts."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        match = self.template.fullmatch(button.custom_id or "")
        if match is None:
            raise ValueError("not a host call control")
        self.card_id = uuid.UUID(match["card"])
        self.press: CallAction = cast(CallAction, match["action"])

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
        await interaction.response.defer()

        async def current_admin(clicker: str) -> bool:
            role, _ = await requester_role(guild, clicker)
            return role is Role.ADMIN

        runtime = bot.runtime
        try:
            press = await press_host_call(
                sessionmaker=runtime.sessionmaker,
                # Off is a rollback: presses on cards already posted are refused too.
                enabled=deployment_on(runtime.settings, "host_call_gate", "discord"),
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild.id)),
                card_id=self.card_id,
                action=self.press,
                clicker=str(interaction.user.id),
                message_id=str(interaction.message.id),
                current_admin=current_admin,
                now=datetime.now(UTC),
            )
        except Exception:
            # Boundary: a failed press must answer the presser, not leave the click hanging.
            log.exception("host_call.press_failed", card_id=str(self.card_id))
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
            view=build_card_view(answered_call_card(press.text, approved=press.approved)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
