"""Requester-only cards for supplied, typed provider action responses."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_GREYPLE
from daimon.core.posted_controls.confirmation import EXPIRED_MESSAGE
from daimon.core.posted_controls.lifecycle import edit_card_within
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.provider_actions import (
    BrowserAuthentication,
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionHook,
    ProviderActionPrompt,
)
from mux.profiles import emits_legacy_events_for

import discord

log = structlog.get_logger(__name__)
_RETIRE_S = 2.0


def _words(
    prompt: ProviderActionPrompt,
) -> tuple[str, tuple[tuple[str, NativeActionResponse], ...]]:
    match prompt.surface:
        case BrowserOriginAccess() as surface:
            origin = discord.utils.escape_mentions(discord.utils.escape_markdown(surface.origin))
            return f"Allow access to {origin}?", (
                ("Approve", surface.approve),
                ("Deny", surface.deny),
                ("Cancel", surface.cancel),
            )
        case BrowserAuthentication() as surface:
            choices = (("Sign-in complete", surface.submit),) if surface.submit is not None else ()
            return "Sign-in required", (*choices, ("Cancel", surface.cancel))
        case _:
            raise TypeError("unsupported provider action surface")


class ProviderActionView(discord.ui.LayoutView):
    """A turn-local view; no native action payload enters a component."""

    def __init__(
        self,
        prompt: ProviderActionPrompt,
        *,
        channel_id: int,
        answer: asyncio.Future[NativeActionResponse | None] | None,
        status: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.prompt = prompt
        self.channel_id = channel_id
        self.answer = answer
        self.choice: str | None = None
        title, choices = _words(prompt)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
            accent_colour=discord.Colour(COLOR_AMBER if answer is not None else COLOR_GREYPLE)
        )
        container.add_item(discord.ui.TextDisplay(f"**{status or title}**"))
        if status is not None:
            container.add_item(discord.ui.TextDisplay(title))
        if answer is not None:
            row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            token = secrets.token_urlsafe(12)
            for index, (label, response) in enumerate(choices):
                button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                    label=label,
                    custom_id=f"dpa:{token}:{index}",
                    style=discord.ButtonStyle.success
                    if label == "Approve"
                    else discord.ButtonStyle.secondary,
                )

                async def callback(
                    interaction: discord.Interaction,
                    selected: NativeActionResponse = response,
                    selected_label: str = label,
                ) -> None:
                    await self.settle(interaction, selected, selected_label)

                button.callback = callback
                row.add_item(button)
            container.add_item(row)
            container.add_item(
                discord.ui.TextDisplay(
                    "-# Only the requester may answer · Expires "
                    f"<t:{int(prompt.context.expires_at.timestamp())}:R>"
                )
            )
        self.add_item(container)

    async def settle(
        self, interaction: discord.Interaction, response: NativeActionResponse, label: str
    ) -> None:
        refusal = None
        if (
            str(interaction.user.id) != self.prompt.context.requester.platform_user_id
            or interaction.channel_id != self.channel_id
        ):
            refusal = "Only the requester can answer this request."
        elif self.answer is None or self.answer.done():
            refusal = "This request is no longer pending."
        elif datetime.now(UTC) >= self.prompt.context.expires_at:
            self.answer.set_result(None)
            refusal = EXPIRED_MESSAGE
        if refusal is not None:
            await interaction.response.send_message(
                refusal, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if self.answer is not None:
            self.choice = label
            self.answer.set_result(response)
        await interaction.response.defer()


def discord_provider_action_hook(
    channel: discord.abc.Messageable, *, thread_id: str, channel_id: int
) -> ProviderActionHook:
    """Only an admitted prompt bound to this Discord destination gets a card."""

    async def request(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
        if (
            prompt.context.requester.platform != "discord"
            or prompt.context.requester.thread_id != thread_id
            or datetime.now(UTC) >= prompt.context.expires_at
        ):
            return None
        answer: asyncio.Future[NativeActionResponse | None] = (
            asyncio.get_running_loop().create_future()
        )
        view = ProviderActionView(prompt, channel_id=channel_id, answer=answer)
        try:
            message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            view.stop()
            answer.cancel()
            raise
        except discord.HTTPException:
            view.stop()
            answer.cancel()
            log.warning("discord.provider_action.post_failed")
            return None
        status = "Expired"
        try:
            remaining = max(0.0, (prompt.context.expires_at - datetime.now(UTC)).total_seconds())
            async with asyncio.timeout(remaining):
                selected = await asyncio.shield(answer)
            if datetime.now(UTC) >= prompt.context.expires_at:
                return None
            if selected is not None:
                status = {
                    "Approve": "Approved",
                    "Deny": "Denied",
                    "Cancel": "Cancelled",
                    "Sign-in complete": "Completed",
                }[view.choice or "Cancel"]
            return selected
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            status = "Expired" if datetime.now(UTC) >= prompt.context.expires_at else "Stopped"
            raise
        finally:
            view.stop()
            if not answer.done():
                answer.cancel()
            await edit_card_within(
                message.edit(
                    view=ProviderActionView(
                        prompt, channel_id=channel_id, answer=None, status=status
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                card_key=("discord", message.id),
                budget_s=_RETIRE_S,
                failure_errors=(discord.HTTPException,),
                failed_event="discord.provider_action.retire_failed",
            )

    return request


class ProviderActionOptions(TypedDict, total=False):
    provider_action: ProviderActionHook


def provider_action_options(
    prepared: PreparedTurn, factory: Callable[[], ProviderActionHook]
) -> ProviderActionOptions:
    ref = prepared.session_ref
    if ref is not None and not emits_legacy_events_for(ref.provider):
        return {"provider_action": factory()}
    return {}
