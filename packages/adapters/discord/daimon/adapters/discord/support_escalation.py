"""SupportEscalateButton + SupportModal -- the human-support path.

A third seeded reaction (`ESCALATE`) sits beside the two vote emoji on every
final answer. It is deliberately a REACTION rather than a button on the answer
itself: `feedback_seed.py` already seeds emoji on that message and
`feedback_reactions.py` already listens, so this reuses a path that exists and
is tested, and the turn's render path is untouched. A real button would mean
every final message carries a persistent View -- a much larger change for the
same affordance. If one-click ever matters more than that, the upgrade is to
add a View here, not to redesign this.

Because a reaction carries no interaction handle, the same bridge
`feedback_button.py` documents applies: react -> direct message carrying a
button -> modal. The custom_id carries `(channel_id, message_id)` rather than a
row id because NO ROW EXISTS YET. Reacting must not spend a credit; only
sending the note does. That is also why the credit gate is evaluated twice --
once here for a cheap early "you're out", and authoritatively inside the write
transaction, which is the one that counts.

Template-disjointness: the `sup:` prefix must never overlap `mfb:` (feedback),
`ztc:` (credential requests) or the wizard's prefixes. discord.py fullmatches
an incoming custom_id against EVERY registered template with no early break,
so an overlap fires two handlers on one click.

Delivery ordering is the load-bearing part of this module. The row is
committed BEFORE any operator DM is attempted, and `delivered_at` is stamped
only once a DM actually lands. A closed-DM operator is the ordinary failure
mode -- `_send_feedback_prompt` already documents that Discord gives no way
around it -- and a support request from a paying trial client that vanishes
because nobody's DMs were open is the one outcome here worth engineering
against. An undelivered row can be swept later; a dropped one cannot.
"""

from __future__ import annotations

import re
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import (
    mark_delivered,
    record_escalation,
)
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import CUSTOM_ID_TEMPLATE, build_custom_id

import discord
from discord.ext import commands

_log = structlog.get_logger()

_CALLBACK_FAILED = "Something went wrong opening the form -- please try again."
_SUBMIT_FAILED = "Something went wrong sending your request -- please try again."
_EMPTY_NOTE = "Please describe what you need help with."
_MALFORMED = "This support request is no longer available."
_OUT_OF_CREDITS = (
    "You've used all your human-support requests. "
    "Contact us if you'd like more added to your account."
)
_RECEIVED = (
    "Thanks -- your request has been recorded and someone will follow up. "
    "You have {remaining} left."
)
_RECORDED_UNDELIVERED = "Thanks -- your request has been recorded and someone will follow up."


class SupportModal(discord.ui.Modal, title="Ask a human"):
    """Free-text form; writing it is what spends the credit.

    `on_submit` commits the row, closes the transaction, and only then tries
    to reach an operator. Holding the transaction open across the DM round
    trips would pin a connection for the duration of an unbounded number of
    HTTP calls, and a failure part-way would roll back a request the person
    has already been told about.
    """

    def __init__(
        self, *, runtime: DiscordRuntime, guild_id: str, channel_id: str, message_id: str
    ) -> None:
        super().__init__()
        self._runtime = runtime
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._message_id = message_id
        self.note_input: discord.ui.TextInput[SupportModal] = discord.ui.TextInput(
            label="What do you need help with?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = str(self.note_input.value or "")
        if not note.strip():
            await interaction.followup.send(_EMPTY_NOTE, ephemeral=True)
            return

        settings = self._runtime.settings
        # The button arrives by direct message, so `interaction.guild_id` is
        # None and the originating guild comes from the custom_id instead.
        guild_id = self._guild_id
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        user_id = str(interaction.user.id)

        async with self._runtime.sessionmaker() as session, session.begin():
            thread_row = await get_latest_thread_session(
                session,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=self._channel_id,
            )
            principal = await find_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="discord",
                external_id=user_id,
            )
            row = await record_escalation(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id if principal is not None else None,
                platform="discord",
                platform_user_id=user_id,
                channel_id=self._channel_id,
                message_id=self._message_id,
                ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
                note=note,
                allowance=settings.support.credits_per_user,
            )

        if row is None:
            _log.info("support.out_of_credits", tenant_id=str(tenant_id))
            await interaction.followup.send(_OUT_OF_CREDITS, ephemeral=True)
            return

        # The row is committed. Everything below is best-effort delivery, and
        # a total failure downgrades the confirmation wording rather than the
        # outcome -- the request is already durable.
        delivered = await self._notify_operators(
            interaction=interaction,
            note=note,
            guild_id=guild_id,
            operator_ids=settings.support.operator_user_ids,
        )
        if delivered:
            async with self._runtime.sessionmaker() as session, session.begin():
                await mark_delivered(session, escalation_id=row.id)

        _log.info(
            "support.escalation_recorded",
            escalation_id=str(row.id),
            tenant_id=str(tenant_id),
            delivered=delivered,
        )
        if not delivered:
            await interaction.followup.send(_RECORDED_UNDELIVERED, ephemeral=True)
            return
        remaining = max(
            settings.support.credits_per_user - (await self._used(tenant_id, user_id)), 0
        )
        await interaction.followup.send(_RECEIVED.format(remaining=remaining), ephemeral=True)

    async def _used(self, tenant_id: Any, user_id: str) -> int:
        from daimon.core.stores.support_escalation import count_escalations_for_user

        async with self._runtime.sessionmaker() as session:
            return await count_escalations_for_user(
                session, tenant_id=tenant_id, platform_user_id=user_id
            )

    async def _notify_operators(
        self,
        *,
        interaction: discord.Interaction,
        note: str,
        guild_id: str,
        operator_ids: list[str],
    ) -> bool:
        """DM every configured operator. True if at least ONE landed.

        Every operator is tried even after one succeeds: they are a rota, not
        a fallback chain, and stopping at the first success would silently
        make the first id in the list the only one who ever hears anything.
        """
        link = f"https://discord.com/channels/{guild_id}/{self._channel_id}/{self._message_id}"
        body = f"**Human support requested** by {interaction.user.mention}\n{link}\n\n{note}"
        bot = cast(commands.Bot, interaction.client)
        delivered = False
        for raw_id in operator_ids:
            try:
                operator = bot.get_user(int(raw_id)) or await bot.fetch_user(int(raw_id))
                await operator.send(body)
                delivered = True
            except (discord.HTTPException, ValueError) as exc:
                # Closed DMs (Forbidden) and a malformed configured id are both
                # operator-side misconfiguration, not the requester's problem.
                _log.warning(
                    "support.operator_undeliverable",
                    operator_id=raw_id,
                    err_type=type(exc).__name__,
                )
        return delivered

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Adapter boundary: discord.py routes on_submit failures here, not to a dispatcher."""
        _log.exception("support_modal.on_error", err_type=type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(_SUBMIT_FAILED, ephemeral=True)
        else:
            await interaction.response.send_message(_SUBMIT_FAILED, ephemeral=True)


class SupportEscalateButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent button delivered by DM; its only job is to open the modal.

    Defines no `interaction_check`, for the reason `feedback_button.py`
    documents: the button arrives in a one-to-one direct message, so Discord's
    own channel membership is the outer boundary, and the custom_id carries no
    user identity to compare a clicker against. The authoritative gate is the
    write path -- the escalation is recorded against the CLICKING user's id and
    counted against their own allowance, so a forwarded custom_id spends the
    forwarder's credit, not the original recipient's.
    """

    def __init__(self, *, guild_id: str, channel_id: str, message_id: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Ask a human",
            custom_id=build_custom_id(
                guild_id=guild_id, channel_id=channel_id, message_id=message_id
            ),
        )
        super().__init__(button)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            guild_id=match["guild_id"],
            channel_id=match["channel_id"],
            message_id=match["message_id"],
        )

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            bot = cast(DaimonBot, interaction.client)
            await interaction.response.send_modal(
                SupportModal(
                    runtime=bot.runtime,
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    message_id=self.message_id,
                )
            )
        except Exception as err:  # noqa: BLE001 -- dynamic-item dispatch is an adapter boundary; discord.py's dispatcher swallows anything raised here
            _log.exception("support_button.callback_failed", err_type=type(err).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(_CALLBACK_FAILED, ephemeral=True)
            else:
                await interaction.response.send_message(_CALLBACK_FAILED, ephemeral=True)
