"""/share-secret and the Reveal button on the DM card (`daimon.core.secret_shares`).

The MCP tool `share_secret_with` posts the same card; this process handles every Reveal.
The value is only in the modal, the encrypted row and the recipient's one ephemeral reply.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.checks import require_registered_guild, resolve_tenant_for_interaction
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import secret_shares as words
from daimon.core.config import env_dm_policy
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores import secret_shares as store

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]
_log = structlog.get_logger()
_FAILED = "That didn't work. Try again."


def _runtime(interaction: discord.Interaction[Any]) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)


async def _sharing_on(runtime: DiscordRuntime, tenant_id: uuid.UUID) -> bool:
    place = FeaturePlace(tenant_id, "discord")
    return (await place_feature(runtime.sessionmaker, runtime.settings, place, "secret_sharing")).on


class SecretShareRevealButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=words.CUSTOM_ID_TEMPLATE
):
    """The card's one button; the row id in its custom_id is all it needs."""

    def __init__(self, *, share_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Reveal",
                style=discord.ButtonStyle.primary,
                custom_id=words.build_custom_id(share_id),
            )
        )
        self.share_id = share_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(share_id=match["share_id"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await reveal(interaction, uuid.UUID(self.share_id))
        except Exception as err:
            # dynamic-item dispatch swallows anything raised here
            _log.exception("secret_share.reveal_failed", err_type=type(err).__name__)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=build_notice_embed(_FAILED), ephemeral=True
                )


async def reveal(interaction: discord.Interaction[commands.Bot], share_id: uuid.UUID) -> None:
    """Claim the value for the clicker, show it once, then turn the card into its record."""
    async with _runtime(interaction).sessionmaker() as session, session.begin():
        got = await store.reveal_share(
            session,
            share_id=share_id,
            platform="discord",
            recipient=str(interaction.user.id),
            now=datetime.now(UTC),
        )
    share = got.share
    if got.value is None or share is None:
        refusal = words.REVEAL_REFUSALS[got.refusal or "gone"]
        await interaction.response.send_message(embed=build_notice_embed(refusal), ephemeral=True)
        if got.refusal == "expired" and share is not None and interaction.message is not None:
            text = words.expired_text(sender=share.sender, label=share.label)
            await interaction.message.edit(embed=build_notice_embed(text, neutral=True), view=None)
        return
    reply = words.reveal_reply(sender=share.sender, label=share.label, value=got.value)
    await interaction.response.send_message(
        embed=build_notice_embed(reply, neutral=True), ephemeral=True
    )
    if interaction.message is not None:
        text = words.revealed_text(
            sender=share.sender, recipient=share.recipient, label=share.label, at=datetime.now(UTC)
        )
        await interaction.message.edit(embed=build_notice_embed(text, neutral=True), view=None)


async def share(
    runtime: DiscordRuntime,
    tenant_id: uuid.UUID,
    *,
    sender: str,
    recipient: discord.Member,
    label: str,
    value: str,
) -> tuple[str, bool]:
    """Store the share and post its card in one transaction; (what to tell the sender, ok)."""
    if not words.value_ok(value):
        return words.BAD_VALUE, False
    if not await _sharing_on(runtime, tenant_id):
        return words.OFF, False
    if not env_dm_policy(runtime.settings.direct_message_policies, tenant_id).allows(
        str(recipient.id)
    ):
        return words.DM_NOT_ALLOWED, False
    try:
        async with runtime.sessionmaker() as session, session.begin():
            row = await store.create_share(
                session,
                tenant_id=tenant_id,
                platform="discord",
                sender=sender,
                recipient=str(recipient.id),
                label=label,
                value=value,
                now=datetime.now(UTC),
            )
            if row is None:
                return words.TOO_MANY, False
            text = words.card_text(
                sender=sender, recipient=str(recipient.id), label=label, expires_at=row.expires_at
            )
            view = discord.ui.View(timeout=None)
            view.add_item(SecretShareRevealButton(share_id=str(row.id)))
            sent = await recipient.send(embed=build_notice_embed(text, neutral=True), view=view)
            await store.set_share_card(
                session, row.id, channel_id=str(sent.channel.id), message_id=str(sent.id)
            )
    except discord.HTTPException:
        return words.dm_closed_text(str(recipient.id)), False
    except store.SecretShareEncryptionRequiredError:
        return words.NO_KEY, False
    return words.shared_text(recipient=str(recipient.id), label=label), True


class ShareSecretModal(discord.ui.Modal, title="Share a secret"):
    def __init__(
        self, runtime: DiscordRuntime, tenant_id: uuid.UUID, recipient: discord.Member, label: str
    ) -> None:
        super().__init__(timeout=600)
        self._runtime, self._tenant_id = runtime, tenant_id
        self._recipient, self._label = recipient, label
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
        if tenant_id is None or not await _sharing_on(runtime, tenant_id):
            refusal = words.OFF
        elif recipient.bot:
            refusal = words.NOT_A_PERSON
        elif cleaned is None:
            refusal = words.BAD_LABEL
        else:
            modal = ShareSecretModal(runtime, tenant_id, recipient, cleaned)
            await interaction.response.send_modal(modal)
            return
        await interaction.response.send_message(embed=build_notice_embed(refusal), ephemeral=True)
