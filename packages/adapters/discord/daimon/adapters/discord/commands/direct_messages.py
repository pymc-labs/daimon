"""Opt-in /dm-me move command and private-message listener."""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal
from weakref import WeakValueDictionary

import anthropic
import structlog
from cachetools import TTLCache
from daimon.adapters.discord.bot import (
    GLOBAL_CAP_NOTICE,
    DaimonBot,
    admission_refusal_message,
    log_anthropic_overload,
)
from daimon.adapters.discord.checks import (
    is_member_daimon_admin,
    is_member_guild_admin,
    require_registered_guild,
)
from daimon.adapters.discord.commands.cold_dm_views import ServerSelector, ServerSwitch
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Action, Subject, authorize
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.direct_messages import (
    ColdDmNotices,
    DirectMessageAccessRevoked,
    admit_cold_dm,
    cold_dm_text,
    load_dm_access_policy,
    reply_to_dm_result,
    require_dm_delivery,
    require_dm_enabled,
    require_unsealed_source,
    start_cold_dm,
    start_dm,
    tenant_ready,
)
from daimon.core.errors import DaimonError
from daimon.core.handoff_context import TranscriptTurn
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.membership_subject import resolve_subject
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.setup_conversations import BoundAgentUnavailable
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    dm_enabled,
    get_conversation,
    get_dm_agent_name,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.tenants import get_tenant, list_tenants_by_platform
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied
from daimon.core.turn.slots import QUEUE_TIMED_OUT_TEXT
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import app_commands
from discord.ext import commands

log = structlog.get_logger(__name__)
_CONVERSATION_TYPES = frozenset({discord.MessageType.default, discord.MessageType.reply})
# DMs never had a per-tenant cap; only the process-wide cap bounds them.
_DM_LANE_CAP = 1_000_000
DISCOVERY_BURST = 3
DISCOVERY_REFILL_SECONDS = 20
SELECTION_SECONDS = 300
_ACCESS_DENIALS: frozenset[AdmissionDenialReason] = frozenset(
    {
        "invoker_not_allowed",
        "writers_none",
        "runs_elsewhere",
        "own_agents_only",
    }
)
_DM_FAILURES = (
    DaimonError,
    MAResolverMissError,
    discord.HTTPException,
    anthropic.APIError,
    SQLAlchemyError,
)
_DISCOVERY_FAILURE_NOTICE = "Couldn't open a private conversation. Please retry or ask an admin."


@dataclass(frozen=True)
class _DmRequest:
    """An explicit switch retains only its route and requester, never an old prompt."""

    id: int
    author: discord.User | discord.Member
    channel: discord.DMChannel
    content: str = ""


@dataclass
class _PendingSelection:
    message: discord.Message | _DmRequest
    expected_scope_id: str | None
    expires_at: float
    selector_id: int | None = None
    view: ServerSelector | None = None


def _error_text(exc: Exception, fallback: str, *, settings: Settings) -> str:
    """Person-facing copy for a failure; an admission refusal is never shown as its code."""
    if isinstance(exc, AdmissionDenied):
        return admission_refusal_message(
            exc.reason, settings, in_dm=True, cause=exc.cause, provider=exc.provider
        )
    return str(exc) if isinstance(exc, DaimonError) else fallback


class DirectMessageCog(commands.Cog):
    def __init__(self, bot: DaimonBot) -> None:
        self.bot = bot
        self._cold_dm_notices = ColdDmNotices()
        self._unloaded = False
        self._locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()
        self._pending: dict[tuple[int, int], _PendingSelection] = {}
        self._switch_views: dict[tuple[int, int], ServerSwitch] = {}
        self._buckets: TTLCache[int, tuple[float, float]] = TTLCache[int, tuple[float, float]](
            10_000, 300
        )

    async def cog_unload(self) -> None:
        self._unloaded = True
        for view in self._switch_views.values():
            view.stop()
        self._switch_views.clear()
        for pending in self._pending.values():
            if pending.view is not None:
                pending.view.complete()
        self._pending.clear()
        self._buckets.clear()

    def _lock(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _claim_discovery(self, user_id: int) -> bool:
        now = monotonic()
        tokens, last = self._buckets.get(user_id, (float(DISCOVERY_BURST), now))
        tokens = min(DISCOVERY_BURST, tokens + (now - last) / DISCOVERY_REFILL_SECONDS)
        allowed = tokens >= 1
        self._buckets[user_id] = (tokens - 1 if allowed else tokens, now)
        if not allowed:
            log.info("discord.dm.discovery_suppressed", user_id=str(user_id), reason="rate_limited")
        return allowed

    def _suppress(self, message: discord.Message | _DmRequest, reason: str) -> None:
        log.info(
            "discord.dm.suppressed",
            user_id=str(message.author.id),
            route_key=str(message.channel.id),
            message_id=str(message.id),
            reason=reason,
        )

    async def _subject(self, tenant_id: uuid.UUID, user_id: int) -> Subject:
        runtime = self.bot.runtime
        return await resolve_subject(
            runtime.sessionmaker,
            runtime.turn_deps.membership_resolver,
            tenant_id=tenant_id,
            user_id=str(user_id),
            require_fresh=True,
        )

    async def _eligible(
        self,
        tenant: TenantRow,
        message: discord.Message | _DmRequest,
    ) -> tuple[Admission | None, bool, bool]:
        runtime = self.bot.runtime
        if not tenant_ready(tenant):
            self._suppress(message, "tenant_unavailable")
            return None, False, False
        async with runtime.sessionmaker() as session:
            enabled = await dm_enabled(session, tenant_id=tenant.id)
            agent = await get_dm_agent_name(session, tenant_id=tenant.id)
            try:
                policy = await load_access_policy(session, tenant_id=tenant.id)
            except AccessPolicyUnreadable:
                self._suppress(message, "policy_unreadable")
                return None, False, False
        recipient_policy = runtime.settings.direct_message_policies.get(
            tenant.id, DirectMessagePolicy()
        )
        if not enabled or agent is None or not recipient_policy.allows(str(message.author.id)):
            self._suppress(message, "dm_policy")
            return None, False, False
        subject = await self._subject(tenant.id, message.author.id)
        if subject.verified_membership is None:
            return None, True, False
        if subject.verified_membership is not True or not authorize(
            policy, subject=subject, action=Action.START_TURN
        ):
            self._suppress(message, "invoker_not_allowed")
            return None, False, False
        try:
            admission = await admit_cold_dm(
                runtime.turn_deps,
                tenant_id=tenant.id,
                external_user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                role=Role.ADMIN if subject.is_admin else Role.USER,
                role_ids=subject.role_ids,
                verified_membership=subject.verified_membership,
            )
        except (DaimonError, MAResolverMissError) as exc:
            if (isinstance(exc, AdmissionDenied) and exc.reason in _ACCESS_DENIALS) or isinstance(
                exc,
                (
                    DirectMessageAccessRevoked,
                    BoundAgentUnavailable,
                    MAResolverMissError,
                    AccessPolicyUnreadable,
                ),
            ):
                self._suppress(
                    message, exc.reason if isinstance(exc, AdmissionDenied) else type(exc).__name__
                )
                return None, False, False
            # Membership and invoker eligibility were established above. Do not
            # disclose which candidate failed, or include exception/resource text.
            log.warning("discord.dm.discovery_failed", error_type=type(exc).__name__)
            await message.channel.send(
                _DISCOVERY_FAILURE_NOTICE, allowed_mentions=discord.AllowedMentions.none()
            )
            return None, False, True
        return admission, False, False

    async def _discover(
        self,
        message: discord.Message | _DmRequest,
        *,
        expected_scope_id: str | None = None,
    ) -> DirectMessageRow | None:
        if self._unloaded or not self._claim_discovery(message.author.id):
            return None
        tenants = await list_tenants_by_platform(self.bot.runtime.sessionmaker, platform="discord")
        candidates: list[tuple[TenantRow, Admission]] = []
        unknown = False
        for tenant in tenants:
            admission, uncertain, failed = await self._eligible(tenant, message)
            if failed:
                return None
            unknown |= uncertain
            if admission is not None:
                candidates.append((tenant, admission))
        if self._unloaded:
            return None
        if unknown:
            await message.channel.send(
                "Server membership is temporarily unavailable. Please retry.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not candidates:
            self._suppress(message, "no_eligible_tenant")
            await self._explain_no_private_chat(message, dms_on=None)
            return None
        if len(candidates) == 1 and expected_scope_id is None:
            tenant, admission = candidates[0]
            return await self._start(message, tenant, admission, expected_scope_id=None)
        key = (message.author.id, message.channel.id)
        pending = _PendingSelection(message, expected_scope_id, monotonic() + SELECTION_SECONDS)
        self._pending[key] = pending

        async def choose(interaction: discord.Interaction, selected: str) -> None:
            async with self._lock(key):
                if (
                    self._pending.get(key) is not pending
                    or monotonic() >= pending.expires_at
                    or interaction.message is None
                    or interaction.message.id != pending.selector_id
                    or interaction.user.id != key[0]
                    or interaction.channel_id != key[1]
                ):
                    await interaction.response.send_message(
                        "This selector expired. Send a new message.", ephemeral=True
                    )
                    return
                if selected not in {str(tenant.id) for tenant, _ in candidates}:
                    await interaction.response.defer()
                    self._suppress(message, "invalid_selection")
                    return
                await interaction.response.defer()
                # Consume before checking: duplicate clicks cannot answer the retained text twice.
                del self._pending[key]
                view.complete()
                await interaction.edit_original_response(view=None)
                async with self.bot.runtime.sessionmaker() as session:
                    tenant = await get_tenant(session, uuid.UUID(selected))
                    current = await get_conversation(
                        session,
                        platform="discord",
                        route_key=str(key[1]),
                        external_user_id=str(key[0]),
                    )
                if (current.scope_id if current else None) != expected_scope_id:
                    self._suppress(message, "selection_superseded")
                    return
                if tenant is None:
                    self._suppress(message, "tenant_unavailable")
                    return
                admission, uncertain, failed = await self._eligible(tenant, message)
                if failed:
                    return
                if uncertain:
                    await message.channel.send(
                        "Server membership is temporarily unavailable. Please retry."
                    )
                    return
                if admission is None:
                    return
                conversation = await self._start(
                    message, tenant, admission, expected_scope_id=expected_scope_id
                )
                if conversation is not None and expected_scope_id is None:
                    await self._reply(message, expected_scope_id=conversation.scope_id)

        servers: list[tuple[str, str]] = []
        for tenant, _ in candidates:
            guild = self.bot.get_guild(int(tenant.external_id))
            servers.append((str(tenant.id), guild.name if guild else tenant.external_id))

        def expire() -> None:
            if self._pending.get(key) is pending:
                del self._pending[key]

        view = ServerSelector(
            user_id=key[0], route_id=key[1], servers=servers, choose=choose, expire=expire
        )
        pending.view = view
        try:
            sent = await message.channel.send(
                "Choose a server for this private conversation.",
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except BaseException:
            expire()
            view.complete()
            raise
        pending.selector_id = sent.id
        view.message = sent
        return None

    async def _start(
        self,
        message: discord.Message | _DmRequest,
        tenant: TenantRow,
        admission: Admission,
        *,
        expected_scope_id: str | None,
    ) -> DirectMessageRow | None:
        conversation = await start_cold_dm(
            self.bot.runtime.turn_deps,
            admission,
            tenant_id=tenant.id,
            workspace_id=tenant.external_id,
            route_key=str(message.channel.id),
            external_user_id=str(message.author.id),
            expected_scope_id=expected_scope_id,
        )
        if conversation is None:
            return None
        guild = self.bot.get_guild(int(tenant.external_id))
        server_name = guild.name if guild else tenant.external_id

        await message.channel.send(
            f"Private conversation with {admission.config.agent_name} in {server_name}.",
            view=self._switch_control(
                user_id=message.author.id,
                route_id=message.channel.id,
                scope_id=conversation.scope_id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return conversation

    def _switch_control(self, *, user_id: int, route_id: int, scope_id: str) -> ServerSwitch:
        async def switch(interaction: discord.Interaction) -> None:
            key = (user_id, route_id)
            async with self._lock(key):
                async with self.bot.runtime.sessionmaker() as session:
                    current = await get_conversation(
                        session,
                        platform="discord",
                        route_key=str(key[1]),
                        external_user_id=str(key[0]),
                    )
                if current is None or current.scope_id != scope_id:
                    view.stop()
                    expire()
                    await interaction.response.send_message(
                        "This conversation changed. Use its latest switch control.", ephemeral=True
                    )
                    return
                pending = self._pending.get(key)
                if pending is not None and monotonic() < pending.expires_at:
                    log.info(
                        "discord.dm.suppressed",
                        user_id=str(user_id),
                        route_key=str(route_id),
                        reason="selection_pending",
                    )
                    await interaction.response.defer()
                    return
                await interaction.response.defer()
                if isinstance(interaction.channel, discord.DMChannel):
                    request = _DmRequest(
                        id=interaction.id, author=interaction.user, channel=interaction.channel
                    )
                    await self._discover(request, expected_scope_id=scope_id)

        key = (user_id, route_id)
        previous = self._switch_views.pop(key, None)
        if previous is not None:
            previous.stop()

        def expire() -> None:
            if self._switch_views.get(key) is view:
                del self._switch_views[key]

        view = ServerSwitch(user_id=user_id, route_id=route_id, switch=switch, expire=expire)
        if self._unloaded:
            view.stop()
        else:
            self._switch_views[key] = view
        return view

    @app_commands.command(name="dm-me", description=DESCRIPTIONS["dm_me"])
    @app_commands.guild_only()
    @require_registered_guild
    async def dm(
        self,
        interaction: discord.Interaction[commands.Bot],
        action: Literal["move", "enable", "disable", "agent", "clear-agent"] = "move",
        agent_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "You can't start a private conversation from here.\n\n"
                "Run /dm-me in the channel you want to continue from.",
                ephemeral=True,
            )
            return
        runtime = self.bot.runtime
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
        try:
            member = await guild.fetch_member(interaction.user.id)
            is_admin = await is_member_daimon_admin(
                member, runtime=self.bot.runtime, action=DelegatedAction.AGENT_SETTINGS
            )
            if action in {"enable", "disable", "agent", "clear-agent"}:
                permitted = (
                    is_member_guild_admin(member, guild_owner_id=guild.owner_id)
                    if action in {"enable", "disable"}
                    else is_admin
                )
                if not permitted:
                    await interaction.followup.send(
                        "Only a platform admin can turn Daimon DMs on or off here.", ephemeral=True
                    )
                    return
                if action in {"agent", "clear-agent"}:
                    if action == "agent" and not agent_name:
                        raise DaimonError("Choose a tenant-owned agent name.")
                    async with runtime.sessionmaker.begin() as session:
                        await set_dm_agent_name(
                            session,
                            tenant_id=tenant_id,
                            agent_name=agent_name if action == "agent" else None,
                        )
                    await interaction.followup.send("Cold DM agent updated.", ephemeral=True)
                    return
                async with runtime.sessionmaker.begin() as session:
                    await set_dm_enabled(session, tenant_id=tenant_id, enabled=action == "enable")
                await interaction.followup.send(
                    "Daimon DMs are on for this server."
                    if action == "enable"
                    else "Daimon DMs are off for this server.",
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
                origin="chat",
                tenant_id=tenant_id,
                requester_org=RequesterOrg.HOST,
                platform="discord",
                external_user_id=str(member.id),
                channel_id=source_channel_id,
                thread_id=str(channel.id) if isinstance(channel, discord.Thread) else None,
                role=Role.ADMIN if is_admin else Role.USER,
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
                f"Continuing from {source_url}.\n\nSend your next message here. "
                "To start a new private conversation, run /dm-me in a channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send("Ready in your DMs.", ephemeral=True)
        except _DM_FAILURES as exc:
            log.warning("discord.dm.move_failed", error_type=type(exc).__name__)
            message = _error_text(
                exc,
                "Couldn't open the conversation. Check that your DMs are open and retry.",
                settings=self.bot.runtime.settings,
            )
            await interaction.followup.send(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            self._unloaded
            or self.bot.draining
            or message.author.bot
            or not isinstance(message.channel, discord.DMChannel)
        ):
            return
        # The whole accepted DM -- slot wait, preparation, the turn and its
        # reply -- is in progress for the shutdown drain, not only the turn.
        with self.bot.runtime.turn_deps.running_turns.track():
            key = (message.author.id, message.channel.id)
            async with self._lock(key):
                pending = self._pending.get(key)
                if pending is not None:
                    if monotonic() < pending.expires_at:
                        self._suppress(message, "selection_pending")
                        return
                    del self._pending[key]
                try:
                    await self._reply(message)
                except _DM_FAILURES as exc:
                    log.warning(
                        "discord.dm.discovery_failed",
                        error_type=type(exc).__name__,
                        user_id=str(message.author.id),
                        reason="discovery_unavailable",
                    )

    async def _explain_no_private_chat(
        self, message: discord.Message | _DmRequest, *, dms_on: bool | None
    ) -> None:
        """Answer a DM that has no conversation to continue, once per person per process."""
        person = str(message.author.id)
        if not self._cold_dm_notices.first_time(person):
            return
        try:
            await message.channel.send(cold_dm_text("discord", dms_on=dms_on))
        except discord.HTTPException as exc:
            self._cold_dm_notices.forget(person)
            log.warning("discord.dm.cold_notice_failed", error_type=type(exc).__name__)

    async def _reply(
        self, message: discord.Message | _DmRequest, *, expected_scope_id: str | None = None
    ) -> None:
        assert isinstance(message.channel, discord.DMChannel)
        runtime = self.bot.runtime
        async with runtime.sessionmaker() as session:
            conversation = await get_conversation(
                session,
                platform="discord",
                route_key=str(message.channel.id),
                external_user_id=str(message.author.id),
            )
        if expected_scope_id is not None and (
            conversation is None or conversation.scope_id != expected_scope_id
        ):
            self._suppress(message, "selection_superseded")
            return
        if conversation is None:
            conversation = await self._discover(message)
            if conversation is None:
                return
        try:
            subject = await self._subject(conversation.tenant_id, message.author.id)
            async with runtime.sessionmaker() as session:
                tenant = await get_tenant(session, conversation.tenant_id)
                policy = await load_dm_access_policy(session, tenant_id=conversation.tenant_id)
                enabled = await dm_enabled(session, tenant_id=conversation.tenant_id)
            recipient_policy = runtime.settings.direct_message_policies.get(
                conversation.tenant_id, DirectMessagePolicy()
            )
            if (
                not enabled
                or not tenant_ready(tenant)
                or not recipient_policy.allows(str(message.author.id))
                or subject.verified_membership is not True
                or not authorize(policy, subject=subject, action=Action.START_TURN)
            ):
                self._suppress(message, "active_access_revoked")
                return
            role = Role.ADMIN if subject.is_admin else Role.USER
            if not message.content.strip():
                await message.channel.send("Send a text message to continue this conversation.")
                return
            # A DM has no card: over the cap it waits under the typing
            # indicator. Only the process-wide cap applies, as before; a
            # tenant's DMs share their own round-robin lane, apart from the
            # server's per-tenant cap.
            ticket = self.bot.turn_queue.admit(
                uuid.uuid5(conversation.tenant_id, "dm"),
                cap=_DM_LANE_CAP,
                tenant_id=str(conversation.tenant_id),
                lane="dm",
                channel_id=str(message.channel.id),
            )
            if ticket is None:
                log.info(
                    "turn.skipped.global_concurrency_shed",
                    tenant_id=str(conversation.tenant_id),
                    path="dm",
                    reason="queue_full",
                )
                await message.channel.send(
                    GLOBAL_CAP_NOTICE, allowed_mentions=discord.AllowedMentions.none()
                )
                return
            try:
                async with message.channel.typing():
                    if await ticket.wait(asyncio.Event()) != "started":
                        await message.channel.send(
                            QUEUE_TIMED_OUT_TEXT, allowed_mentions=discord.AllowedMentions.none()
                        )
                        return
                    reply = await reply_to_dm_result(
                        runtime.turn_deps,
                        platform="discord",
                        route_key=str(message.channel.id),
                        external_user_id=str(message.author.id),
                        message_id=str(message.id),
                        expected_scope_id=conversation.scope_id,
                        text=message.content,
                        role=role,
                        role_ids=subject.role_ids,
                        verified_membership=subject.verified_membership,
                        confirmation_for=lambda admission: discord_confirmation_hook(
                            message.channel,
                            runtime=runtime,
                            tenant_id=conversation.tenant_id,
                            account_id=admission.account_id,
                            admission=admission,
                        ),
                    )
            finally:
                ticket.release()
            if reply is not None:
                answer = reply.text
                for start in range(0, len(answer), 1900):
                    # Each transport await is another opportunity for revocation.
                    await require_dm_delivery(runtime.turn_deps, reply)
                    if not runtime.settings.direct_message_policies.get(
                        conversation.tenant_id, DirectMessagePolicy()
                    ).allows(str(message.author.id)):
                        self._suppress(message, "delivery_recipient_revoked")
                        return
                    chunk = answer[start : start + 1900]
                    if start + 1900 >= len(answer):
                        await message.channel.send(
                            chunk,
                            view=self._switch_control(
                                user_id=message.author.id,
                                route_id=message.channel.id,
                                scope_id=conversation.scope_id,
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        await message.channel.send(
                            chunk, allowed_mentions=discord.AllowedMentions.none()
                        )
        except _DM_FAILURES as exc:
            log_anthropic_overload(
                exc,
                tenant_id=conversation.tenant_id,
                path="dm",
                alert_webhook_url=self.bot.runtime.settings.ops.alert_webhook_url,
            )
            log.warning("discord.dm.turn_failed", error_type=type(exc).__name__)
            if isinstance(exc, DirectMessageAccessRevoked):
                self._suppress(message, exc.reason)
                return
            if isinstance(exc, AdmissionDenied) and exc.reason in _ACCESS_DENIALS:
                self._suppress(message, exc.reason)
                return
            error = _error_text(
                exc,
                "Couldn't verify or complete this private conversation. Please retry.",
                settings=self.bot.runtime.settings,
            )
            with contextlib.suppress(discord.HTTPException):
                await message.channel.send(error, allowed_mentions=discord.AllowedMentions.none())
