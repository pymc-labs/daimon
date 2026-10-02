"""Opt-in /dm move command and private-message listener."""

import contextlib
from datetime import UTC, datetime
from typing import Literal

import anthropic
import structlog
from daimon.adapters.discord.bot import (
    CHANNEL_BUDGET_NOTICE,
    GLOBAL_CAP_NOTICE,
    DaimonBot,
    log_anthropic_overload,
)
from daimon.adapters.discord.checks import (
    is_member_guild_admin,
    member_role_ids,
    require_registered_guild,
)
from daimon.core.direct_messages import (
    reply_to_dm,
    require_dm_enabled,
    require_unsealed_source,
    start_dm,
)
from daimon.core.errors import DaimonError
from daimon.core.handoff_context import TranscriptTurn
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.direct_messages import dm_enabled, get_conversation, set_dm_enabled
from daimon.core.stores.domain import Role
from daimon.core.turn.admission import admit
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import app_commands
from discord.ext import commands

log = structlog.get_logger(__name__)
_CONVERSATION_TYPES = frozenset({discord.MessageType.default, discord.MessageType.reply})


def _error_text(exc: Exception, fallback: str) -> str:
    if isinstance(exc, AdmissionDenied) and exc.reason == "channel_budget_exceeded":
        return "Sorry, " + CHANNEL_BUDGET_NOTICE
    return str(exc) if isinstance(exc, DaimonError) else fallback


_DM_DENIAL_COPY: dict[AdmissionDenialReason, str] = {
    "agent_pinned_elsewhere": (
        "This channel's agent only runs in the channels an operator pinned it to, "
        "so it can't continue in a DM."
    ),
    "invoker_not_allowed": (
        "You aren't on this workspace's list of people who can start a turn. An admin can add you."
    ),
    "channel_protected": "This channel is protected, so it can't be moved to a DM.",
    "channel_isolated": (
        "This channel is isolated, so its conversations stay in it and can't move to a DM."
    ),
    "balance_depleted": "This workspace's daimon credit is depleted. An admin can top up.",
    "cap_exceeded": "The monthly usage cap is reached. An admin can adjust it.",
    "channel_budget_exceeded": "Sorry, " + CHANNEL_BUDGET_NOTICE,
}


def _dm_denial_message(exc: AdmissionDenied) -> str:
    """Person-facing copy for a /dm refused at admission (the raw reason is a code)."""
    return _DM_DENIAL_COPY.get(exc.reason, "This conversation can't be moved to a DM.")


class DirectMessageCog(commands.Cog):
    def __init__(self, bot: DaimonBot) -> None:
        self.bot = bot

    @app_commands.command(name="dm", description="Continue this conversation privately")
    @app_commands.guild_only()
    @require_registered_guild
    async def dm(
        self,
        interaction: discord.Interaction[commands.Bot],
        action: Literal["move", "enable", "disable"] = "move",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "Run /dm in a server text channel or thread.", ephemeral=True
            )
            return
        runtime = self.bot.runtime
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
        try:
            member = await guild.fetch_member(interaction.user.id)
            is_admin = is_member_guild_admin(member, guild_owner_id=guild.owner_id)
            if action in {"enable", "disable"}:
                if not is_admin:
                    await interaction.followup.send(
                        "That needs someone with Manage Server.", ephemeral=True
                    )
                    return
                async with runtime.sessionmaker.begin() as session:
                    await set_dm_enabled(session, tenant_id=tenant_id, enabled=action == "enable")
                await interaction.followup.send(
                    "DM conversations enabled."
                    if action == "enable"
                    else "DM conversations disabled.",
                    ephemeral=True,
                )
                return
            await require_dm_enabled(runtime.turn_deps, tenant_id=tenant_id)
            permissions = channel.permissions_for(member)
            if not permissions.view_channel or not permissions.read_message_history:
                raise DaimonError("You need access to this channel's history to move it to DMs.")
            parent_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
            source_channel_id = str(parent_id or channel.id)
            admission = await admit(
                runtime.turn_deps,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id=str(member.id),
                channel_id=source_channel_id,
                thread_id=str(channel.id) if isinstance(channel, discord.Thread) else None,
                role=Role.ADMIN if is_admin else Role.USER,
                platform_role_ids=member_role_ids(member),
                is_dm=True,
                dm_source_channel_id=source_channel_id,
                now=datetime.now(UTC),
            )
            require_unsealed_source(admission)
            messages = [message async for message in channel.history(limit=12)]
            context = [
                TranscriptTurn(
                    role="agent"
                    if self.bot.user is not None and message.author.id == self.bot.user.id
                    else "user",
                    text=f"{message.author.display_name}: {message.content}",
                )
                for message in reversed(messages)
                # System notices (thread created, pins, joins) are not the
                # conversation, and a thread-created notice names the thread.
                if message.content and message.type in _CONVERSATION_TYPES
            ]
            dm_channel = await member.create_dm()
            source_url = f"https://discord.com/channels/{guild.id}/{channel.id}"
            await start_dm(
                runtime.turn_deps,
                admission,
                tenant_id=tenant_id,
                platform="discord",
                workspace_id=str(guild.id),
                route_key=str(dm_channel.id),
                channel_id=str(dm_channel.id),
                external_user_id=str(member.id),
                source_url=source_url,
                source_channel_id=source_channel_id,
                source_thread_id=str(channel.id) if isinstance(channel, discord.Thread) else None,
                context=context,
            )
            await dm_channel.send(
                f"Continuing from {source_url}. Send your next message here. "
                "Run /dm again in a server channel to start a new private conversation.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send("Ready in your DMs.", ephemeral=True)
        except (
            DaimonError,
            MAResolverMissError,
            discord.HTTPException,
            anthropic.APIError,
            SQLAlchemyError,
        ) as exc:
            log.warning("discord.dm.move_failed", error_type=type(exc).__name__)
            message = (
                _dm_denial_message(exc)
                if isinstance(exc, AdmissionDenied)
                else str(exc)
                if isinstance(exc, DaimonError)
                else "Couldn't open the conversation. Check that your DMs are open and retry."
            )
            await interaction.followup.send(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            self.bot.draining
            or message.author.bot
            or not isinstance(message.channel, discord.DMChannel)
        ):
            return
        runtime = self.bot.runtime
        async with runtime.sessionmaker() as session:
            conversation = await get_conversation(
                session,
                platform="discord",
                route_key=str(message.channel.id),
                external_user_id=str(message.author.id),
            )
            if conversation is None or not await dm_enabled(
                session, tenant_id=conversation.tenant_id
            ):
                return
        try:
            guild = self.bot.get_guild(int(conversation.workspace_id))
            if guild is None:
                raise DaimonError("This workspace is no longer available.")
            # Live fetch also proves membership. A departed user never falls back
            # to a stored role or keeps using a former workspace's credentials.
            member = await guild.fetch_member(message.author.id)
            role = (
                Role.ADMIN
                if is_member_guild_admin(member, guild_owner_id=guild.owner_id)
                else Role.USER
            )
            if not message.content.strip():
                await message.channel.send("Send a text message to continue this conversation.")
                return
            if not self.bot.try_claim_global_turn():
                await message.channel.send(
                    GLOBAL_CAP_NOTICE, allowed_mentions=discord.AllowedMentions.none()
                )
                return
            try:
                async with message.channel.typing():
                    answer = await reply_to_dm(
                        runtime.turn_deps,
                        platform="discord",
                        route_key=str(message.channel.id),
                        external_user_id=str(message.author.id),
                        message_id=str(message.id),
                        expected_scope_id=conversation.scope_id,
                        text=message.content,
                        role=role,
                        platform_role_ids=member_role_ids(member),
                    )
            finally:
                self.bot.release_global_turn()
            if answer is not None:
                for start in range(0, len(answer), 1900):
                    await message.channel.send(
                        answer[start : start + 1900],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except (
            DaimonError,
            MAResolverMissError,
            discord.HTTPException,
            anthropic.APIError,
            SQLAlchemyError,
        ) as exc:
            log_anthropic_overload(
                exc,
                tenant_id=conversation.tenant_id,
                path="dm",
                alert_webhook_url=self.bot.runtime.settings.ops.alert_webhook_url,
            )
            log.warning("discord.dm.turn_failed", error_type=type(exc).__name__)
            error = _error_text(
                exc, "Couldn't verify or complete this private conversation. Please retry."
            )
            with contextlib.suppress(discord.HTTPException):
                await message.channel.send(error, allowed_mentions=discord.AllowedMentions.none())
