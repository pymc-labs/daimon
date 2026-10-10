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

Requests land in a CHANNEL, not in operator direct messages. A channel
survives one person's DMs being closed, leaves a shared record anyone on the
rota can pick up, and does not silently make whoever is first in a config list
the only person who ever hears anything.

A channel with its own admins is the exception: its requests go to those
admins by DM first, then to the server admins, and to the channel only when
no DM landed (`daimon.core.support_routing`).

Delivery ordering is the load-bearing part of this module. The row is
committed BEFORE the post is attempted, and `delivered_at` is stamped only
once it actually lands. A support request from a paying trial client that
vanishes because the channel was misconfigured or the bot lost access is the
one outcome here worth engineering against. An undelivered row can be swept
later; a dropped one cannot.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.channel_admin_roles import member_roles
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.core.config import DirectMessagePolicy, SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import (
    mark_delivered,
    record_escalation,
)
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    ASK_THE_TEAM,
    CUSTOM_ID_TEMPLATE,
    EMPTY_NOTE,
    ESCALATE,
    OUT_OF_CREDITS,
    RECORDED_UNDELIVERED,
    UNAVAILABLE,
    build_custom_id,
    received_text,
)
from daimon.core.support_routing import support_recipient_tiers

import discord
from discord.ext import commands

_log = structlog.get_logger()

_CALLBACK_FAILED = "That didn't work. Try again."
_SUBMIT_FAILED = "That didn't work. Try again."
# The person-facing copy is shared with Slack (`daimon.core.support_escalation`).
_EMPTY_NOTE = EMPTY_NOTE
_MALFORMED = UNAVAILABLE
_OUT_OF_CREDITS = OUT_OF_CREDITS
_RECORDED_UNDELIVERED = RECORDED_UNDELIVERED


def discord_channel(support: SupportSettings) -> str | None:
    """Ask a human's escalation channel when the Discord bot can post in it, else None.

    Teams once shared `escalation_channel_id`; its channel now has its own
    setting. A Teams id (`19:…`) left here from then is not Discord's, so
    Discord offers no Ask a human rather than spending credits on requests it
    could never deliver.
    """
    channel = support.escalation_channel_id
    return None if channel is None or channel.startswith("19:") else channel


async def post_to_support_channel(
    bot: commands.Bot,
    *,
    channel_id: str,
    body: str,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> bool:
    """Post ``body`` into the support channel. True if it landed.

    Shared by Ask a human and the routed 👎 form (`feedback_modal`). Resolves
    through the cache first and falls back to one fetch, because the
    escalation channel lives in the operators' own guild and may not be in a
    freshly-started bot's cache. Returns False on anything that means the
    message did not arrive -- a channel that cannot be resolved, one the bot
    cannot post in, or an id that is not a channel it can message.
    """
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.abc.Messageable):
            _log.warning("support.channel_not_messageable", channel_id=channel_id)
            return False
        for chunk in split_for_discord_safe(body):
            if allowed_mentions is None:
                await channel.send(chunk)
            else:
                await channel.send(chunk, allowed_mentions=allowed_mentions)
        return True
    except (discord.HTTPException, ValueError) as exc:
        # A misconfigured id, a channel the bot was removed from, or lost
        # send permission. All operator-side, none of them the requester's
        # problem -- and none of them may lose the row.
        _log.warning(
            "support.channel_undeliverable",
            channel_id=channel_id,
            err_type=type(exc).__name__,
        )
        return False


class SupportModal(discord.ui.Modal, title=ASK_THE_TEAM):
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
            # Discord caps a label at 45 characters; Slack says this in its label.
            placeholder="Write a few words.",
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
        channel_id = discord_channel(settings.support)
        if channel_id is None:
            # Disabled between the reaction and the submit. Nothing is spent.
            await interaction.followup.send(_MALFORMED, ephemeral=True)
            return
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
        delivered = await self._dm_admins(
            interaction=interaction, note=note, tenant_id=tenant_id
        ) or await self._post_to_channel(
            interaction=interaction,
            note=note,
            guild_id=guild_id,
            channel_id=channel_id,
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
        await interaction.followup.send(received_text(remaining=remaining), ephemeral=True)

    async def _used(self, tenant_id: uuid.UUID, user_id: str) -> int:
        from daimon.core.stores.support_escalation import count_escalations_for_user

        async with self._runtime.sessionmaker() as session:
            return await count_escalations_for_user(
                session, tenant_id=tenant_id, platform_user_id=user_id
            )

    def _body(self, interaction: discord.Interaction, note: str) -> str:
        link = (
            f"https://discord.com/channels/{self._guild_id}/{self._channel_id}/{self._message_id}"
        )
        return (
            f"**Human support requested** by {interaction.user.mention} "
            f"({interaction.user})\n{link}\n\n{note}"
        )

    async def _origin_channel_id(self, bot: commands.Bot) -> str:
        """The answer's channel, or a thread's parent, which is what a grant names."""
        try:
            channel = bot.get_channel(int(self._channel_id)) or await bot.fetch_channel(
                int(self._channel_id)
            )
        except (discord.HTTPException, ValueError):
            return self._channel_id
        if isinstance(channel, discord.Thread) and channel.parent_id:
            return str(channel.parent_id)
        return self._channel_id

    async def _dm_admins(
        self, *, interaction: discord.Interaction, note: str, tenant_id: uuid.UUID
    ) -> bool:
        """DM the origin channel's admins, else the server admins. True once a tier got it.

        False at once for a channel with no admins of its own, so it keeps the
        escalation channel. Each DM is held to the tenant's DM policy and goes
        only to a current human member of the guild.
        """
        bot = cast(DaimonBot, interaction.client)
        tiers = await support_recipient_tiers(
            self._runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=await self._origin_channel_id(bot),
            requester_id=str(interaction.user.id),
            members=member_roles(self._runtime, bot, self._guild_id),
        )
        if not tiers:
            return False
        policies = self._runtime.settings.direct_message_policies
        policy = policies.get(tenant_id, DirectMessagePolicy())
        body = self._body(interaction, note)
        chunks = split_for_discord_safe(body)
        for tier in tiers:
            landed = 0
            for user_id in (uid for uid in tier if policy.allows(uid)):
                try:
                    dm = await bot.open_member_dm(int(self._guild_id), int(user_id))
                    for chunk in chunks:
                        await dm.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                    landed += 1
                except (discord.HTTPException, LookupError, ValueError) as exc:
                    _log.info("support.admin_dm_undelivered", err_type=type(exc).__name__)
            if landed:
                _log.info("support.sent_to_admins", recipients=landed)
                return True
        return False

    async def _post_to_channel(
        self,
        *,
        interaction: discord.Interaction,
        note: str,
        guild_id: str,
        channel_id: str,
    ) -> bool:
        """Post the request into the escalation channel. True if it landed; on
        False the caller leaves `delivered_at` NULL and tells the requester the
        honest thing."""
        return await post_to_support_channel(
            cast(commands.Bot, interaction.client),
            channel_id=channel_id,
            body=self._body(interaction, note),
        )

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
            label=ASK_THE_TEAM,
            emoji=ESCALATE,
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
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary; discord.py's dispatcher swallows
            # anything raised here
            _log.exception("support_button.callback_failed", err_type=type(err).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(_CALLBACK_FAILED, ephemeral=True)
            else:
                await interaction.response.send_message(_CALLBACK_FAILED, ephemeral=True)
