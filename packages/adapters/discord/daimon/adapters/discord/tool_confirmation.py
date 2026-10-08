"""Discord's `ConfirmationHook`: post a confirmation card, wait for its button.

The card's words and states come from `daimon.core.posted_controls.confirmation`;
this module only draws them as a components-v2 `LayoutView` with Approve and
Deny, and turns the click into a `ConfirmationAnswer`.

The view is non-persistent (VIEW-04): the turn waiting on the answer lives in
this process, so a live View instance holds the future and no custom_id or
`DynamicItem` registration is needed. If the process restarts, the turn that
was waiting dies with it and the card's buttons simply stop answering.
"""

from __future__ import annotations

import asyncio
import re
import secrets

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_GREEN, COLOR_GREYPLE
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationHook, ConfirmationPrompt
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import settle_local_confirmation, wait_local_confirmation

import discord

__all__ = ["build_confirmation_view", "discord_confirmation_hook"]

log = structlog.get_logger(__name__)

#: Most time a card edit may take while retiring it.
RETIRE_TIMEOUT_S = 2.0


def _body(card: ConfirmationCard) -> list[str]:
    lines = [f"**{_safe_text(card.headline)}**"]
    if card.body:
        lines.append(_safe_text(card.body))
    if card.consequence:
        lines.append(_safe_text(card.consequence))
    return lines


def _safe_text(value: str) -> str:
    """Keep tool-provided words literal in Discord text displays and replies."""
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    return re.sub(r"<#(?=\d+>)", "<#\u200b", escaped)


def _footer(card: ConfirmationCard, prompt: ConfirmationPrompt) -> str | None:
    if card.footer is None:
        return None
    return card.footer.format(
        requester=f"<@{prompt.requester_platform_user_id}>",
        expires=f"<t:{int(prompt.expires_at.timestamp())}:R>",
    )


class _ConfirmationView(discord.ui.LayoutView):
    """One card; resolves `answer` on the requester's first click."""

    def __init__(
        self,
        card: ConfirmationCard,
        prompt: ConfirmationPrompt,
        answer: asyncio.Future[ConfirmationAnswer] | None,
    ) -> None:
        super().__init__(timeout=None)
        self._prompt = prompt
        self._answer = answer
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
            accent_colour=discord.Colour(
                COLOR_AMBER
                if card.state == "pending"
                else COLOR_GREEN
                if card.state == "approved"
                else COLOR_GREYPLE
            )
        )
        for text in _body(card):
            container.add_item(discord.ui.TextDisplay(text))
        if card.state == "pending":
            container.add_item(discord.ui.Separator())
            row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            approve: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.success, label="Approve"
            )
            deny: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.danger, label="Deny"
            )
            details: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.secondary, label="Details"
            )
            approve.callback = self._on_approve
            deny.callback = self._on_deny
            details.callback = self._on_details
            row.add_item(approve)
            row.add_item(deny)
            row.add_item(details)
            container.add_item(row)
        footer = _footer(card, prompt)
        if footer is not None:
            for line in footer.splitlines():
                container.add_item(discord.ui.TextDisplay(f"-# {line}"))
        self.add_item(container)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "approved")

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "denied")

    async def _on_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "\n".join(_safe_text(line) for line in self._prompt.detail_lines)
            or "No additional details.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _settle(self, interaction: discord.Interaction, answer: ConfirmationAnswer) -> None:
        refusal = settle_local_confirmation(
            self._prompt, self._answer, str(interaction.user.id), answer
        )
        if refusal is not None:
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(requester=f"<@{self._prompt.requester_platform_user_id}>")
            await interaction.response.send_message(
                refusal, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        # Answered: nothing on this card listens any more.
        self.stop()
        answered = build_confirmation_card(
            self._prompt, state=answer, answered_by_platform_user_id=str(interaction.user.id)
        )
        await interaction.response.edit_message(
            view=_ConfirmationView(answered, self._prompt, answer=None),
            allowed_mentions=discord.AllowedMentions.none(),
        )


def build_confirmation_view(
    card: ConfirmationCard,
    prompt: ConfirmationPrompt,
    answer: asyncio.Future[ConfirmationAnswer] | None = None,
) -> discord.ui.LayoutView:
    """The components-v2 view for `card`; buttons only while pending."""
    return _ConfirmationView(card, prompt, answer)


def discord_confirmation_hook(channel: discord.abc.Messageable) -> ConfirmationHook:
    """A hook that posts each prompt's card into `channel` (the turn's thread)."""

    async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[ConfirmationAnswer] = loop.create_future()
        pending = build_confirmation_card(prompt, state="pending", token=secrets.token_urlsafe(12))
        view = _ConfirmationView(pending, prompt, answer)
        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())

        async def retire(state: ConfirmationCardState) -> None:
            await _retire(message, prompt, state)

        return await wait_local_confirmation(prompt, answer, stop=view.stop, retire=retire)

    return _confirm


async def _retire(
    message: discord.Message, prompt: ConfirmationPrompt, state: ConfirmationCardState
) -> None:
    card = build_confirmation_card(prompt, state=state)
    try:
        # Bounded: retiring is cosmetic, and runs while a turn is being
        # stopped or timed out — a slow Discord must not hold that up.
        await asyncio.wait_for(
            message.edit(
                view=_ConfirmationView(card, prompt, answer=None),
                allowed_mentions=discord.AllowedMentions.none(),
            ),
            timeout=RETIRE_TIMEOUT_S,
        )
    except (discord.HTTPException, TimeoutError) as err:
        log.warning("tool_confirmation.retire_failed", error=str(err) or type(err).__name__)
