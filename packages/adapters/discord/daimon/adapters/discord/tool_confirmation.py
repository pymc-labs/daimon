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
import secrets
from datetime import UTC, datetime

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationHook, ConfirmationPrompt
from daimon.core.posted_controls.confirmation import (
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    build_confirmation_card,
)

import discord

__all__ = ["build_confirmation_view", "discord_confirmation_hook"]

log = structlog.get_logger(__name__)

_DETAIL_MAX = 1800

#: Most time a card edit may take while retiring it.
RETIRE_TIMEOUT_S = 2.0


def _body(card: ConfirmationCard) -> list[str]:
    lines = [f"**{card.headline}**"]
    if card.fields:
        lines.append("\n".join(f"-# {label}: `{value}`" for label, value in card.fields))
    if card.detail:
        lines.append(f"```json\n{card.detail[:_DETAIL_MAX]}\n```")
    return lines


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
            accent_colour=discord.Colour(COLOR_AMBER)
        )
        for text in _body(card):
            container.add_item(discord.ui.TextDisplay(text))
        if card.state == "pending":
            container.add_item(discord.ui.Separator(visible=False))
            row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            approve: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.success, label="Approve"
            )
            deny: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.danger, label="Deny"
            )
            approve.callback = self._on_approve
            deny.callback = self._on_deny
            row.add_item(approve)
            row.add_item(deny)
            container.add_item(row)
        footer = _footer(card, prompt)
        if footer is not None:
            container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        self.add_item(container)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "approved")

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "denied")

    async def _settle(self, interaction: discord.Interaction, answer: ConfirmationAnswer) -> None:
        if str(interaction.user.id) != self._prompt.requester_platform_user_id:
            await interaction.response.send_message(NOT_YOURS_MESSAGE, ephemeral=True)
            return
        if self._answer is None or self._answer.done():
            await interaction.response.send_message(NO_LONGER_PENDING_MESSAGE, ephemeral=True)
            return
        self._answer.set_result(answer)
        # Answered: nothing on this card listens any more.
        self.stop()
        answered = build_confirmation_card(
            self._prompt, state=answer, answered_by_platform_user_id=str(interaction.user.id)
        )
        await interaction.response.edit_message(
            view=_ConfirmationView(answered, self._prompt, answer=None)
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
        message = await channel.send(view=view)
        timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
        try:
            result = await asyncio.wait_for(asyncio.shield(answer), timeout=timeout_s)
        except TimeoutError:
            result = "expired"
        except asyncio.CancelledError:
            # The turn was stopped while the card was up: retire its buttons.
            answer.cancel()
            view.stop()
            await _retire(message, prompt, "denied")
            raise
        if result == "expired":
            answer.cancel()
            view.stop()
            await _retire(message, prompt, "expired")
        return result

    return _confirm


async def _retire(
    message: discord.Message, prompt: ConfirmationPrompt, state: ConfirmationAnswer
) -> None:
    card = build_confirmation_card(prompt, state=state)
    try:
        # Bounded: retiring is cosmetic, and runs while a turn is being
        # stopped or timed out — a slow Discord must not hold that up.
        await asyncio.wait_for(
            message.edit(view=_ConfirmationView(card, prompt, answer=None)),
            timeout=RETIRE_TIMEOUT_S,
        )
    except (discord.HTTPException, TimeoutError) as err:
        log.warning("tool_confirmation.retire_failed", error=str(err) or type(err).__name__)
