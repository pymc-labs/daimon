"""/share-secret, and the Reveal and Send buttons on the DM cards (`daimon.core.secret_shares`).

The MCP tool `share_secret_with` posts the same cards; this process handles every click.
The value is only in the modal, the encrypted row and the recipient's one ephemeral reply.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.channel_agent import channel_agent_id
from daimon.adapters.discord.checks import require_registered_guild, resolve_tenant_for_interaction
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import secret_share_flow as flow
from daimon.core import secret_shares as words
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores import secret_shares as store
from daimon.core.stores.secret_shares import ShareView

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]
_log = structlog.get_logger()
_FAILED = "That didn't work. Try again."


def _runtime(interaction: discord.Interaction[Any]) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)


def _reply(text: str, *, neutral: bool = False) -> dict[str, Any]:
    return {"embed": build_notice_embed(text, neutral=neutral), "ephemeral": True}


def _reveal_view(tenant_id: uuid.UUID, share_id: uuid.UUID) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SecretShareRevealButton(tenant_id=str(tenant_id), share_id=str(share_id)))
    return view


class _ShareButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=words.CUSTOM_ID_TEMPLATE
):
    """A share card's one button; the tenant and row ids in its custom_id are all it needs."""

    label: str = ""
    confirm: bool = False

    def __init__(self, *, tenant_id: str, share_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=self.label,
                style=discord.ButtonStyle.primary,
                custom_id=words.build_custom_id(tenant_id, share_id, confirm=self.confirm),
            )
        )
        self.tenant_id, self.share_id = tenant_id, share_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(tenant_id=match["tenant_id"], share_id=match["share_id"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        # Deferred first: the reply is a followup, so a slow read never loses a reveal.
        await interaction.response.defer(ephemeral=True, thinking=True)
        step = confirm if self.confirm else reveal
        try:
            await step(interaction, uuid.UUID(self.tenant_id), uuid.UUID(self.share_id))
        except Exception as err:
            # dynamic-item dispatch swallows anything raised here
            _log.exception("secret_share.click_failed", err_type=type(err).__name__)
            await interaction.followup.send(**_reply(_FAILED))


class SecretShareRevealButton(_ShareButton, template=words.CUSTOM_ID_TEMPLATE):
    label = "Reveal"


class SecretShareConfirmButton(_ShareButton, template=words.CONFIRM_ID_TEMPLATE):
    label = "Send"
    confirm = True


async def reveal(
    interaction: discord.Interaction[commands.Bot], tenant_id: uuid.UUID, share_id: uuid.UUID
) -> None:
    """Claim the value for the clicker, show it once, then turn the card into its record.
    The reply is sent before the claim commits, so a failed send keeps the value."""
    runtime = _runtime(interaction)
    clicker = str(interaction.user.id)
    async with runtime.sessionmaker() as session, session.begin():
        found = await store.get_share(
            session, tenant_id=tenant_id, share_id=share_id, platform="discord"
        )
        refusal = None
        if found is not None and found.recipient == clicker:
            refusal = await flow.sharing_refusal(
                session,
                runtime.settings,
                tenant_id=tenant_id,
                recipient=clicker,
                agent_id=found.agent_id,
            )
        got = (
            None
            if refusal
            else await store.reveal_share(
                session,
                tenant_id=tenant_id,
                share_id=share_id,
                platform="discord",
                recipient=clicker,
                now=datetime.now(UTC),
            )
        )
        if got is not None and got.value is not None and got.share is not None:
            reply = words.reveal_reply(
                sender=got.share.sender, label=got.share.label, value=got.value
            )
            await interaction.followup.send(**_reply(reply, neutral=True))
    if got is None or got.value is None or got.share is None:
        text = refusal or words.REVEAL_REFUSALS[(got and got.refusal) or "gone"]
        await interaction.followup.send(**_reply(text))
        if got is not None and got.refusal == "expired" and got.share is not None:
            expired = words.expired_text(sender=got.share.sender, label=got.share.label)
            await _retire(interaction, expired)
        return
    share = got.share
    text = words.revealed_text(
        sender=share.sender, recipient=share.recipient, label=share.label, at=datetime.now(UTC)
    )
    await _retire(interaction, text)


async def confirm(
    interaction: discord.Interaction[commands.Bot], tenant_id: uuid.UUID, share_id: uuid.UUID
) -> None:
    """The sender's Send on a share held for them: post the recipient's card, once."""
    runtime = _runtime(interaction)
    async with runtime.sessionmaker() as session, session.begin():
        found = await store.get_share(
            session, tenant_id=tenant_id, share_id=share_id, platform="discord"
        )
        refusal = None
        if found is not None and found.sender == str(interaction.user.id):
            refusal = await flow.sharing_refusal(
                session,
                runtime.settings,
                tenant_id=tenant_id,
                recipient=found.recipient,
                agent_id=found.agent_id,
            )
        got = (
            None
            if refusal
            else await store.confirm_share(
                session,
                tenant_id=tenant_id,
                share_id=share_id,
                platform="discord",
                sender=str(interaction.user.id),
                now=datetime.now(UTC),
            )
        )
    if got is None or got.refusal is not None or got.share is None:
        await interaction.followup.send(
            **_reply(refusal or words.REVEAL_REFUSALS[(got and got.refusal) or "gone"])
        )
        return
    share = got.share

    async def send(view_of: ShareView) -> tuple[str, str]:
        user = await interaction.client.fetch_user(int(view_of.recipient))
        text = words.card_text(
            sender=view_of.sender,
            recipient=view_of.recipient,
            label=view_of.label,
            expires_at=view_of.expires_at,
        )
        sent = await user.send(
            embed=build_notice_embed(text, neutral=True), view=_reveal_view(tenant_id, view_of.id)
        )
        return str(sent.channel.id), str(sent.id)

    try:
        await flow.post_card(runtime.sessionmaker, share, send)
    except discord.HTTPException:
        await interaction.followup.send(**_reply(words.dm_closed_text(share.recipient)))
        return
    done = words.shared_text(recipient=share.recipient, label=share.label)
    await interaction.followup.send(**_reply(done, neutral=True))
    await _retire(interaction, done)


async def _retire(interaction: discord.Interaction[commands.Bot], text: str) -> None:
    """Turn the clicked card into its record, without its button. Best effort."""
    if interaction.message is None:
        return
    try:
        await interaction.message.edit(embed=build_notice_embed(text, neutral=True), view=None)
    except discord.HTTPException as err:
        _log.warning("secret_share.card_edit_failed", err_type=type(err).__name__)


async def _refusal(
    runtime: DiscordRuntime, tenant_id: uuid.UUID, recipient: str, agent_id: str | None
) -> str | None:
    async with runtime.sessionmaker() as session:
        return await flow.sharing_refusal(
            session, runtime.settings, tenant_id=tenant_id, recipient=recipient, agent_id=agent_id
        )


async def share(
    runtime: DiscordRuntime,
    tenant_id: uuid.UUID,
    *,
    sender: str,
    recipient: discord.Member,
    label: str,
    value: str,
    agent_id: str | None,
) -> tuple[str, bool]:
    """Store the share, then post its card; (what to tell the sender, ok)."""
    if not words.value_ok(value):
        return words.BAD_VALUE, False
    if refusal := await _refusal(runtime, tenant_id, str(recipient.id), agent_id):
        return refusal, False

    async def send(view_of: ShareView) -> tuple[str, str]:
        text = words.card_text(
            sender=sender, recipient=view_of.recipient, label=label, expires_at=view_of.expires_at
        )
        sent = await recipient.send(
            embed=build_notice_embed(text, neutral=True), view=_reveal_view(tenant_id, view_of.id)
        )
        return str(sent.channel.id), str(sent.id)

    try:
        row = await flow.offer_share(
            runtime.sessionmaker,
            send,
            tenant_id=tenant_id,
            sender=sender,
            recipient=str(recipient.id),
            label=label,
            value=value,
            now=datetime.now(UTC),
            agent_id=agent_id,
        )
    except discord.HTTPException:
        return words.dm_closed_text(str(recipient.id)), False
    except store.SecretShareEncryptionRequiredError:
        return words.NO_KEY, False
    if row is None:
        return words.TOO_MANY, False
    return words.shared_text(recipient=str(recipient.id), label=label), True


class ShareSecretModal(discord.ui.Modal, title="Share a secret"):
    def __init__(
        self,
        runtime: DiscordRuntime,
        tenant_id: uuid.UUID,
        recipient: discord.Member,
        label: str,
        agent_id: str | None,
    ) -> None:
        super().__init__(timeout=600)
        self._runtime, self._tenant_id = runtime, tenant_id
        self._recipient, self._label, self._agent_id = recipient, label, agent_id
        self.secret: discord.ui.TextInput[ShareSecretModal] = discord.ui.TextInput(
            label="Secret",
            style=discord.TextStyle.paragraph,
            max_length=1700,  # words.MAX_VALUE; a literal for the modal lint
        )
        self.add_item(self.secret)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, ok = await share(
            self._runtime,
            self._tenant_id,
            sender=str(interaction.user.id),
            recipient=self._recipient,
            label=self._label,
            value=str(self.secret.value or ""),
            agent_id=self._agent_id,
        )
        await interaction.followup.send(embed=build_notice_embed(text, neutral=ok), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, /) -> None:
        _log.exception("secret_share.submit_failed", err_type=type(error).__name__)
        embed = build_notice_embed(_FAILED)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.guild_only()
class ShareSecretCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="share-secret", description=DESCRIPTIONS["share_secret"])
    @app_commands.describe(recipient="Who gets it", label="What they see, like the service's name")
    @require_registered_guild
    async def share_secret(
        self,
        interaction: BotInteraction,
        recipient: discord.Member,
        label: app_commands.Range[str, 1, words.MAX_LABEL],
    ) -> None:
        runtime = _runtime(interaction)
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        cleaned = words.clean_label(label)
        agent_id = None
        if tenant_id is not None:
            agent_id = await channel_agent_id(runtime, tenant_id, str(interaction.channel_id))
        if tenant_id is None:
            refusal = words.OFF
        elif recipient.bot:
            refusal = words.NOT_A_PERSON
        elif cleaned is None:
            refusal = words.BAD_LABEL
        elif refusal := await _refusal(runtime, tenant_id, str(recipient.id), agent_id):
            pass
        else:
            modal = ShareSecretModal(runtime, tenant_id, recipient, cleaned, agent_id)
            await interaction.response.send_modal(modal)
            return
        await interaction.response.send_message(embed=build_notice_embed(refusal), ephemeral=True)
