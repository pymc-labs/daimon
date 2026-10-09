"""FeedbackModal -- the single-field free-text form the feedback button opens.

Opened from `FeedbackButton.callback` after a click on a persistent button
delivered into a direct message. The form collects exactly ONE field: the
free-text criticism itself. There is no category picker and no second input
-- the row being annotated is already fixed by the `feedback_id` carried in
the button's `custom_id`, so the user never retypes anything else.

Write path: `on_submit` defers ephemerally, rejects whitespace-only input
server-side (the client already enforces `required=True`, but that is not
trusted alone), then calls `daimon.core.stores.message_feedback.
attach_feedback_text`, which gates the write on the clicking user's own
`platform_user_id`. A `None` return covers both "the row was purged" and
"this row belongs to someone else" -- the reply text deliberately does not
distinguish the two, so a click carrying someone else's row id learns
nothing about whether that row exists.

Logging discipline: the submitted text is somebody's unsolicited criticism,
not a secret, but it belongs in the database row. It never enters a log
record, a `custom_id` or an embed. Every log line below carries the feedback
row id only. The one other place it may go is the support channel, and only
for a tenant that turned that on (`SupportSettings.feedback_to_support`):
the prompt that offered this form said so, and each submission is posted
there once, beside the person, the agent and a link to the answer. This mirrors
the hygiene guarantees `credential_modals.py` already documents for a
different reason (there it is secrecy; here it is that this is unsolicited,
personal criticism that should not multiply across the observability
pipeline).
"""

from __future__ import annotations

import uuid
from typing import cast

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.support_escalation import post_to_support_channel
from daimon.core.config import SupportSettings
from daimon.core.stores.domain import MessageFeedbackRow
from daimon.core.stores.message_feedback import attach_feedback_text
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session

import discord
from discord.ext import commands

_log = structlog.get_logger()

_EMPTY_TEXT = "Write a few words first."
_NO_LONGER_AVAILABLE = "This request has expired."
_SUBMIT_FAILED = "That didn't work. Try again."
_THANKS = "Thanks for the feedback."


class FeedbackModal(discord.ui.Modal, title="What went wrong?"):
    """Single-field free-text form, written through the ownership-predicated store call."""

    def __init__(self, *, runtime: DiscordRuntime, feedback_id: uuid.UUID) -> None:
        super().__init__()
        self._runtime = runtime
        self._feedback_id = feedback_id
        self.text_input: discord.ui.TextInput[FeedbackModal] = discord.ui.TextInput(
            label="What went wrong?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_text = str(self.text_input.value or "")

        if not raw_text.strip():
            await interaction.followup.send(_EMPTY_TEXT, ephemeral=True)
            return

        async with self._runtime.sessionmaker() as session, session.begin():
            updated_row = await attach_feedback_text(
                session,
                feedback_id=self._feedback_id,
                platform_user_id=str(interaction.user.id),
                feedback_text=raw_text,
            )

        if updated_row is None:
            _log.info("feedback_modal.no_longer_available", feedback_id=str(self._feedback_id))
            await interaction.followup.send(_NO_LONGER_AVAILABLE, ephemeral=True)
            return

        _log.info("feedback_modal.submit", feedback_id=str(self._feedback_id))
        channel_id = support_channel_for(self._runtime.settings.support, updated_row.tenant_id)
        if channel_id is not None:
            # Best-effort: the text is already recorded, whatever the post does.
            delivered = await post_to_support_channel(
                cast(commands.Bot, interaction.client),
                channel_id=channel_id,
                body=await self._support_post(interaction, updated_row),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            _log.info(
                "feedback.routed_to_support",
                feedback_id=str(self._feedback_id),
                delivered=delivered,
            )
        await interaction.followup.send(_THANKS, ephemeral=True)

    async def _support_post(self, interaction: discord.Interaction, row: MessageFeedbackRow) -> str:
        """Who, a link to the answer, the agent, and the text. Not the answer itself."""
        async with self._runtime.sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
            thread_row = await get_latest_thread_session(
                session, tenant_id=row.tenant_id, platform="discord", thread_id=row.channel_id
            )
        who = f"{interaction.user.mention} ({interaction.user})"
        lines = [f"**\N{THUMBS DOWN SIGN} Feedback** from {who}"]
        if tenant is not None:
            lines.append(
                f"https://discord.com/channels/{tenant.external_id}/{row.channel_id}/{row.message_id}"
            )
        else:
            lines.append(f"message {row.message_id} in channel {row.channel_id}")
        agent_id = thread_row.ma_agent_id if thread_row is not None else None
        session_id = row.ma_session_id or (
            thread_row.ma_session_id if thread_row is not None else None
        )
        if agent_id or session_id:
            lines.append(f"Agent `{agent_id or 'unknown'}`, session `{session_id or 'unknown'}`")
        return "\n".join(lines) + "\n\n" + discord.utils.escape_mentions(row.feedback_text or "")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Adapter-boundary catch-all: a modal submission has no other error surface.

        Unlike `from_custom_id`/`interaction_check`/`callback` on a
        `DynamicItem` (see `feedback_button.py`), discord.py does NOT swallow
        exceptions raised from `on_submit` -- it routes them here instead. But
        leaving the default `on_error` behavior (log only) would still leave
        the person staring at a form that silently failed, so this overrides
        it to also send an apology.
        """
        _log.exception("feedback_modal.on_error", err_type=type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(_SUBMIT_FAILED, ephemeral=True)
        else:
            await interaction.response.send_message(_SUBMIT_FAILED, ephemeral=True)


def support_channel_for(support: SupportSettings, tenant_id: uuid.UUID) -> str | None:
    """The Discord channel this tenant's submitted forms also go to, or None (the default).

    Ask a human's `escalation_channel_id`, unless it still holds a Teams
    channel (`19:…`) from before Teams had its own setting. Anything but a configured
    string channel and a literal True reads as off.
    """
    channel = cast(object, support.escalation_channel_id)
    if not isinstance(channel, str) or not channel or channel.startswith("19:"):
        return None
    return channel if cast(object, support.routes_feedback(tenant_id)) is True else None
