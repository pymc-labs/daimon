"""Discord's `ConfirmationHook`: post a confirmation card, wait for its button.

The card's words and states come from `daimon.core.posted_controls.confirmation`;
this module only draws them as a components-v2 `LayoutView` with Approve and
Deny, and turns the click into a `ConfirmationAnswer`.

The view is non-persistent: the turn waiting on the answer lives in
this process, so a live View instance holds the future and no custom_id or
`DynamicItem` registration is needed. If the process restarts, the turn that
was waiting dies with it and the card's buttons simply stop answering.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_GREEN, COLOR_GREYPLE
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import (
    check_confirmation_admin,
    edit_card_within,
    settle_local_confirmation,
    wait_local_confirmation,
)

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.runtime import DiscordRuntime

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
        check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._prompt = prompt
        self._answer = answer
        self._check_admin = check_admin
        self.answered_edit_done = asyncio.Event()
        #: The posted card's ordering key for `edit_card_within`; set once the
        #: card is sent, before anyone can click it.
        self.card_key: object = ("discord-view", id(self))
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
        if prompt.admin:
            for text in card.detail_lines:
                container.add_item(discord.ui.TextDisplay(_safe_text(text)))
        if card.state == "pending":
            container.add_item(discord.ui.Separator())
            row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            approve: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.success, label="Confirm" if prompt.admin else "Approve"
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
        refusal = await check_confirmation_admin(
            self._prompt, str(interaction.user.id), self._check_admin
        )
        if refusal is not None:
            await interaction.response.send_message(
                refusal, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
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
        # Same per-card order as `_retire`, registered before any await, so a
        # Stopped retire that follows can never land before this answer.
        try:
            await edit_card_within(
                interaction.response.edit_message(
                    view=_ConfirmationView(answered, self._prompt, answer=None),
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                card_key=self.card_key,
                budget_s=RETIRE_TIMEOUT_S,
                failure_errors=(discord.HTTPException,),
                failed_event="tool_confirmation.answer_edit_failed",
            )
        finally:
            self.answered_edit_done.set()


def build_confirmation_view(
    card: ConfirmationCard,
    prompt: ConfirmationPrompt,
    answer: asyncio.Future[ConfirmationAnswer] | None = None,
) -> discord.ui.LayoutView:
    """The components-v2 view for `card`; buttons only while pending."""
    return _ConfirmationView(card, prompt, answer)


def discord_confirmation_hook(
    channel: discord.abc.Messageable,
    *,
    runtime: DiscordRuntime | None = None,
    tenant_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    prepare_admin: Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]] | None = None,
    check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
) -> ConfirmationHook:
    """A hook that posts each prompt's card into `channel` (the turn's thread)."""

    if runtime is not None:
        from daimon.core.channel_backend import backend_selection_enabled, make_backend_service

        if backend_selection_enabled(runtime.settings.turn):
            from daimon.adapters.discord.bot import requester_role
            from daimon.core.confirmation import backend_switch_callbacks
            from daimon.core.stores.domain import Role

            if tenant_id is None or account_id is None:
                raise ValueError("Backend confirmation requires an admitted account and tenant")

            async def current_admin(clicker: str) -> bool:
                if not isinstance(channel, (discord.Thread, discord.TextChannel)):
                    return False
                role, _ = await requester_role(channel.guild, clicker)
                return role is Role.ADMIN

            prepare_admin, check_admin = backend_switch_callbacks(
                make_backend_service(runtime.sessionmaker, runtime.settings.turn),
                sessionmaker=runtime.sessionmaker,
                anthropic=runtime.anthropic,
                tenant_id=tenant_id,
                account_id=account_id,
                current_admin=current_admin,
            )

    async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
        if prompt.admin:
            if prepare_admin is None or check_admin is None:
                return "denied"
            prompt = await prepare_admin(prompt)
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[ConfirmationAnswer] = loop.create_future()
        pending = build_confirmation_card(prompt, state="pending", token=secrets.token_urlsafe(12))
        view = _ConfirmationView(pending, prompt, answer, check_admin)
        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        view.card_key = ("discord", message.id)

        async def retire(state: ConfirmationCardState) -> None:
            await _retire(message, prompt, state)

        result = await wait_local_confirmation(prompt, answer, stop=view.stop, retire=retire)
        if result == "approved":

            async def retire_unsent() -> None:
                # Queued behind the Approved edit on the same card.
                await retire("stopped")

            return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
        return result

    return _confirm


async def _retire(
    message: discord.Message, prompt: ConfirmationPrompt, state: ConfirmationCardState
) -> None:
    card = build_confirmation_card(prompt, state=state)
    # Bounded for the turn, finished in the background (`edit_card_within`).
    await edit_card_within(
        message.edit(
            view=_ConfirmationView(card, prompt, answer=None),
            allowed_mentions=discord.AllowedMentions.none(),
        ),
        card_key=("discord", message.id),
        budget_s=RETIRE_TIMEOUT_S,
        failure_errors=(discord.HTTPException,),
        failed_event="tool_confirmation.retire_failed",
    )
