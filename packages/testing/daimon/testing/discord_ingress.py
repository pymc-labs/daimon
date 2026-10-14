"""Opt-in offline Discord gateway ingress; no routing/context or native roots invented.

Requires the Discord workspace package. Feed original MESSAGE_CREATE payloads,
then observe the actual SDK thread-create, HTTP posts and production queue state.
The runner owns the native wire/runtime and its independent completion oracle.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core._models import ProviderBindingSlot, ThreadLease, TurnCardIntent
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.stores.accounts import get_account
from daimon.core.stores.mux_state import get_binding
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import list_live_thread_sessions
from daimon.core.stores.usage_events import list_for_tenant
from daimon.testing.discord_runtime_capture import (
    require_discord_runtime_capture as _require_runtime_bound,
)
from daimon.testing.discord_surface import DiscordPostEvent, OfflineDiscordGateway
from daimon.testing.ma_transport import ScriptedTransport
from discord.http import HTTPClient, MultipartParameters
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import Provider, Scope, ThreadRef
from mux.state.lease import Slot
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

if TYPE_CHECKING:
    from discord.types.channel import TextChannel as ChannelPayload
    from discord.types.gateway import MessageCreateEvent
    from discord.types.guild import Guild as GuildPayload
    from discord.types.message import Message as MessagePayload
    from discord.types.threads import Thread as ThreadPayload
    from discord.types.user import User as UserPayload


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class GatewayMessage(Observation):
    order: int = Field(ge=0)
    message_id: str
    channel_id: str
    observed_s: float = Field(ge=0)
    payload_json: str


class ThreadCreation(Observation):
    channel_id: str
    message_id: str
    thread_id: str
    name: str
    observed_s: float = Field(ge=0)
    request_json: str
    response_json: str


class HistoryRead(Observation):
    channel_id: str
    observed_s: float = Field(ge=0)
    request_json: str
    response_json: str


class NamingCall(Observation):
    request_json: str


class NamingMeter(Observation):
    platform_user_id: str
    managed_session_id: str
    model: str
    input_tokens: int
    output_tokens: int


class PendingThread(Observation):
    thread_id: str
    message_ids: tuple[str, ...]


class ReactionEvent(Observation):
    order: int = Field(ge=0)
    operation: Literal["add", "remove"]
    channel_id: str
    message_id: str
    emoji: str
    observed_s: float = Field(ge=0)


class ParsedReply(Observation):
    """Reference facts read from discord.py's actual scheduled Message object."""

    message_id: str
    channel_id: str
    reference_message_id: str | None
    resolved_message_id: str | None
    resolved_channel_id: str | None
    resolved_author_id: str | None


class MessageFetch(Observation):
    channel_id: str
    message_id: str
    observed_s: float = Field(ge=0)
    response_json: str


class DiscordIngressReceipt(Observation):
    offline_only: Literal[True] = True
    backend_revision: ConfigRevision
    guild_id: str
    parent_channel_id: str
    gateway_messages: tuple[GatewayMessage, ...]
    dispatched_message_ids: tuple[str, ...]
    thread_creations: tuple[ThreadCreation, ...]
    posts: tuple[DiscordPostEvent, ...]
    naming_calls: tuple[NamingCall, ...]
    history_reads: tuple[HistoryRead, ...]
    naming_meters: tuple[NamingMeter, ...]
    callbacks_pending: int
    queue_depth: int
    queue_in_flight: int
    processing_thread_ids: tuple[str, ...]
    pending_threads: tuple[PendingThread, ...]
    errors: tuple[str, ...]
    reaction_history: tuple[ReactionEvent, ...] = ()
    parsed_replies: tuple[ParsedReply, ...] = ()
    message_fetches: tuple[MessageFetch, ...] = ()
    # A native root/terminal fact must come from the runner's actual wire gate,
    # not from the title response or this ingress receipt.
    completion_certificate: Literal[False] = False


class IngressHTTP:
    """Owned HTTP fixture; history is original gateway payloads, never XML.

    Threads exist only after actual discord.py thread creation. A runner can
    hold the first actual post to inspect admission/queue state before any
    native turn is allocated. Default sends return immediately.
    """

    def __init__(
        self,
        *,
        evidence_id: str,
        parent_channel_id: int,
        clock: Callable[[], float] = time.monotonic,
        hold_first_post: bool = False,
    ) -> None:
        self.evidence_id, self.parent_channel_id, self.clock = evidence_id, parent_channel_id, clock
        self.surfaces = {
            parent_channel_id: OfflineDiscordGateway(
                evidence_id=f"{evidence_id}:{parent_channel_id}",
                channel_id=parent_channel_id,
                clock=clock,
            )
        }
        self.history: dict[int, list[dict[str, Any]]] = {parent_channel_id: []}
        self.creations: list[ThreadCreation] = []
        self.posts: list[DiscordPostEvent] = []
        self.history_reads: list[HistoryRead] = []
        self.reactions: list[tuple[str, int, int, str]] = []
        self.reaction_history: list[ReactionEvent] = []
        self.message_fetches: list[MessageFetch] = []
        self.first_post = asyncio.Event()
        self.release_first_post = asyncio.Event()
        if not hold_first_post:
            self.release_first_post.set()
        self.guild_id = 0
        self.bot_user_id = 0
        self.connection_state: Any = None  # discord.py ConnectionState is an SDK boundary

    def surface(self, channel_id: int) -> OfflineDiscordGateway:
        if channel_id not in self.surfaces:
            raise ValueError("unowned Discord HTTP destination")
        return self.surfaces[channel_id]

    async def start_thread_with_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        name: str,
        auto_archive_duration: int,
        rate_limit_per_user: int | None = None,
        reason: str | None = None,
    ) -> ThreadPayload:
        if channel_id != self.parent_channel_id or self.connection_state is None:
            raise ValueError("unowned thread creation")
        if not any(str(p["id"]) == str(message_id) for p in self.history[channel_id]):
            raise ValueError("thread creation has no original gateway message")
        if message_id in self.surfaces:
            raise ValueError("thread already exists for that message")
        # Discord uses the originating message snowflake for this thread ID.
        payload = {
            "id": str(message_id),
            "guild_id": str(self.guild_id),
            "parent_id": str(channel_id),
            "owner_id": str(self.bot_user_id),
            "name": name,
            "type": 11,
            "message_count": 0,
            "member_count": 1,
            "rate_limit_per_user": rate_limit_per_user or 0,
            "thread_metadata": {
                "archived": False,
                "auto_archive_duration": auto_archive_duration,
                "archive_timestamp": datetime.now(UTC).isoformat(),
                "locked": False,
                "invitable": True,
            },
        }
        self.creations.append(
            ThreadCreation(
                channel_id=str(channel_id),
                message_id=str(message_id),
                thread_id=str(message_id),
                name=name,
                observed_s=self.clock(),
                request_json=json.dumps(
                    {
                        "name": name,
                        "auto_archive_duration": auto_archive_duration,
                        "rate_limit_per_user": rate_limit_per_user,
                        "reason": reason,
                    }
                ),
                response_json=json.dumps(payload),
            )
        )
        self.surfaces[message_id] = OfflineDiscordGateway(
            evidence_id=f"{self.evidence_id}:{message_id}", channel_id=message_id, clock=self.clock
        )
        self.history[message_id] = []
        # The fake gateway uses the real parser/cache path for the thread event.
        self.connection_state.parse_thread_create({**payload, "newly_created": True})
        return cast("ThreadPayload", json.loads(json.dumps(payload)))

    async def send_message(self, channel_id: int, *, params: MultipartParameters) -> MessagePayload:
        surface = self.surface(channel_id)
        result = await surface.send_message(channel_id, params=params)
        self.posts.append(surface.events[-1])
        if not self.first_post.is_set():
            self.first_post.set()
            await self.release_first_post.wait()
        return result

    async def edit_message(
        self, channel_id: int, message_id: int, *, params: MultipartParameters
    ) -> MessagePayload:
        surface = self.surface(channel_id)
        result = await surface.edit_message(channel_id, message_id, params=params)
        self.posts.append(surface.events[-1])
        return result

    async def delete_message(self, channel_id: int, message_id: int, **kwargs: object) -> None:
        surface = self.surface(channel_id)
        await surface.delete_message(channel_id, message_id, **kwargs)
        self.posts.append(surface.events[-1])

    async def get_message(self, channel_id: int, message_id: int) -> MessagePayload:
        self.surface(channel_id)
        for payload in self.history[channel_id]:
            if int(payload["id"]) == message_id:
                result = cast("MessagePayload", json.loads(json.dumps(payload)))
                break
        else:
            result = await self.surface(channel_id).get_message(channel_id, message_id)
        self.message_fetches.append(
            MessageFetch(
                channel_id=str(channel_id),
                message_id=str(message_id),
                observed_s=self.clock(),
                response_json=json.dumps(result),
            )
        )
        return result

    async def logs_from(
        self,
        channel_id: int,
        limit: int,
        *,
        before: int | None = None,
        after: int | None = None,
        around: int | None = None,
    ) -> list[MessagePayload]:
        self.surface(channel_id)
        if around is not None:
            raise ValueError("around-history is not bound in this slice")
        rows = [
            p
            for p in self.history[channel_id]
            if (before is None or int(p["id"]) < before) and (after is None or int(p["id"]) > after)
        ]
        result = list(reversed(rows))[:limit]
        self.history_reads.append(
            HistoryRead(
                channel_id=str(channel_id),
                observed_s=self.clock(),
                request_json=json.dumps({"limit": limit, "before": before, "after": after}),
                response_json=json.dumps(result),
            )
        )
        return cast("list[MessagePayload]", json.loads(json.dumps(result)))

    async def send_typing(self, channel_id: int) -> None:
        self.surface(channel_id)

    async def add_reaction(
        self, channel_id: int, message_id: int, emoji: str, **kwargs: object
    ) -> None:
        self.surface(channel_id)
        self.reactions.append(("add", channel_id, message_id, emoji))
        self.reaction_history.append(
            ReactionEvent(
                order=len(self.reaction_history),
                operation="add",
                channel_id=str(channel_id),
                message_id=str(message_id),
                emoji=emoji,
                observed_s=self.clock(),
            )
        )

    async def remove_own_reaction(
        self, channel_id: int, message_id: int, emoji: str, **kwargs: object
    ) -> None:
        self.surface(channel_id)
        self.reactions.append(("remove", channel_id, message_id, emoji))
        self.reaction_history.append(
            ReactionEvent(
                order=len(self.reaction_history),
                operation="remove",
                channel_id=str(channel_id),
                message_id=str(message_id),
                emoji=emoji,
                observed_s=self.clock(),
            )
        )

    async def edit_channel(self, channel_id: int, **kwargs: object) -> dict[str, Any]:
        self.surface(channel_id)
        thread = next((row for row in self.creations if row.thread_id == str(channel_id)), None)
        if thread is None or set(kwargs) - {"archived", "reason"}:
            raise ValueError("unbound channel edit")
        payload = json.loads(thread.response_json)
        payload["thread_metadata"]["archived"] = kwargs.get("archived", False)
        return payload


class _ObservedBot(DaimonBot):
    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        intents: discord.Intents,
        http_client: HTTPClient,
        clock: Callable[[], datetime] | None,
        native: ScriptedTransport,
    ) -> None:
        self.native = native
        _require_runtime_bound(runtime, native)
        super().__init__(runtime=runtime, intents=intents, http_client=http_client, clock=clock)
        self.message_tasks: list[asyncio.Task[None]] = []
        self.dispatched_ids: list[str] = []
        self.ingress_errors: list[str] = []
        self.parsed_replies: list[ParsedReply] = []

    async def on_message(
        self,
        message: discord.Message,
        *,
        startup_replay: bool = False,
        resume_admitted: bool = False,
    ) -> None:
        # Gateway callbacks are scheduled: an owner can replace deps after deliver.
        _require_runtime_bound(self.runtime, self.native)
        reference = message.reference
        if isinstance(reference, discord.MessageReference):
            resolved = reference.resolved
            self.parsed_replies.append(
                ParsedReply(
                    message_id=str(message.id),
                    channel_id=str(message.channel.id),
                    reference_message_id=str(reference.message_id)
                    if reference.message_id is not None
                    else None,
                    resolved_message_id=str(resolved.id)
                    if isinstance(resolved, discord.Message)
                    else None,
                    resolved_channel_id=str(resolved.channel.id)
                    if isinstance(resolved, discord.Message)
                    else None,
                    resolved_author_id=str(resolved.author.id)
                    if isinstance(resolved, discord.Message)
                    else None,
                )
            )
        await super().on_message(
            message, startup_replay=startup_replay, resume_admitted=resume_admitted
        )

    def _schedule_event(
        self,
        coro: Callable[..., Coroutine[Any, Any, Any]],
        event_name: str,
        *args: object,
        **kwargs: object,
    ) -> asyncio.Task[None]:
        task = cast(asyncio.Task[None], super()._schedule_event(coro, event_name, *args, **kwargs))  # pyright: ignore[reportUnknownMemberType]
        if event_name == "on_message":
            self.message_tasks.append(task)
            self.dispatched_ids.append(str(cast(discord.Message, args[0]).id))
        return task

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        self.ingress_errors.append(event_method)


class DiscordIngress:
    """Single owned bot; messages are delivered through the gateway parser.

    Scope authorizes this offline fixture's ownership, not synthetic native root
    identity. Normal product admission still decides the actual caller account.
    """

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        scope: Scope,
        http: IngressHTTP,
        native: ScriptedTransport,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_runtime_bound(runtime, native)
        if scope.is_platform or scope.is_legacy_host_authorized:
            raise ValueError("explicit tenant scope required")
        if not scope.principal_id.strip() or not scope.authorization_id.strip():
            raise ValueError("explicit offline authorization required")
        self.runtime, self.scope, self.http, self.native = runtime, scope, http, native
        self.bot = _ObservedBot(
            runtime=runtime,
            intents=discord.Intents.none(),
            http_client=cast(HTTPClient, http),
            clock=utc_clock,
            native=native,
        )
        self.gateway_messages: list[GatewayMessage] = []
        self._opened = False
        self._closed = False
        self._opening_lock = asyncio.Lock()
        self._backend_revision: ConfigRevision | None = None
        self._native_bound()

    def _native_bound(self) -> None:
        _require_runtime_bound(self.bot.runtime, self.native)

    async def open(
        self,
        *,
        guild: Mapping[str, object],
        channel: Mapping[str, object],
        bot_user: Mapping[str, object],
    ) -> None:
        async with self._opening_lock:
            await self._open(guild=guild, channel=channel, bot_user=bot_user)

    async def _open(
        self,
        *,
        guild: Mapping[str, object],
        channel: Mapping[str, object],
        bot_user: Mapping[str, object],
    ) -> None:
        self._native_bound()
        if (
            self._opened
            or self._closed
            or self.http.creations
            or self.http.posts
            or self.native.requests
            or any(self.http.history.values())
            or set(self.http.surfaces) != {self.http.parent_channel_id}
        ):
            raise ValueError("ingress needs fresh owned boundaries")
        tenant_id, account_id = uuid.UUID(self.scope.tenant_id), uuid.UUID(self.scope.account_id)
        async with self.bot.runtime.sessionmaker() as session:
            tenant, account = (
                await get_tenant(session, tenant_id),
                await get_account(session, account_id),
            )
            backend = await current_backend(
                session, channel_ref(tenant_id, "discord", str(channel["id"]))
            )
        if (
            tenant is None
            or account is None
            or account.tenant_id != tenant_id
            or tenant.platform != "discord"
            or tenant.external_id != str(guild["id"])
            or backend is None
        ):
            raise ValueError("ingress destination is not genuinely owned/configured")
        if str(channel["id"]) != str(self.http.parent_channel_id):
            raise ValueError("HTTP parent differs from actual gateway channel")
        await self.bot._async_setup_hook()  # pyright: ignore[reportPrivateUsage]
        state = self.bot._connection  # pyright: ignore[reportPrivateUsage]
        state.user = discord.ClientUser(state=state, data=cast("UserPayload", dict(bot_user)))
        actual_guild = discord.Guild(state=state, data=cast("GuildPayload", dict(guild)))
        actual_channel = discord.TextChannel(
            state=state, guild=actual_guild, data=cast("ChannelPayload", dict(channel))
        )
        state._add_guild(actual_guild)  # pyright: ignore[reportPrivateUsage]
        actual_guild._add_channel(actual_channel)  # pyright: ignore[reportPrivateUsage]
        self.http.connection_state, self.http.guild_id, self.http.bot_user_id = (
            state,
            actual_guild.id,
            state.user.id,
        )
        self._backend_revision = backend
        self._opened = True

    def deliver(self, payload: Mapping[str, object]) -> None:
        self._native_bound()
        if (
            self._closed
            or not self._opened
            or str(payload.get("guild_id")) != str(self.http.guild_id)
        ):
            raise ValueError("message has no owned gateway guild")
        channel_id = int(str(payload["channel_id"]))
        self.http.surface(channel_id)
        raw = json.loads(json.dumps(dict(payload)))
        # Preserve exact source order and raw identity, including equal-text
        # messages and re-delivered events. No sorting, dedupe or XML injection.
        self.gateway_messages.append(
            GatewayMessage(
                order=len(self.gateway_messages),
                message_id=str(payload["id"]),
                channel_id=str(channel_id),
                observed_s=self.http.clock(),
                payload_json=json.dumps(raw),
            )
        )
        self.http.history[channel_id].append(raw)
        self.bot._connection.parse_message_create(cast("MessageCreateEvent", raw))  # pyright: ignore[reportPrivateUsage]

    async def wait(self) -> None:
        await asyncio.gather(*self.bot.message_tasks)

    async def receipt(self) -> DiscordIngressReceipt:
        self._native_bound()
        async with self.bot.runtime.sessionmaker() as session:
            revision = await current_backend(
                session,
                channel_ref(
                    uuid.UUID(self.scope.tenant_id), "discord", str(self.http.parent_channel_id)
                ),
            )
            rows = await list_for_tenant(session, tenant_id=uuid.UUID(self.scope.tenant_id))
        if not self._opened or revision is None or revision != self._backend_revision:
            raise ValueError("owned backend configuration disappeared or changed")
        return DiscordIngressReceipt(
            backend_revision=revision,
            guild_id=str(self.http.guild_id),
            parent_channel_id=str(self.http.parent_channel_id),
            gateway_messages=tuple(self.gateway_messages),
            dispatched_message_ids=tuple(self.bot.dispatched_ids),
            thread_creations=tuple(self.http.creations),
            posts=tuple(self.http.posts),
            history_reads=tuple(self.http.history_reads),
            naming_calls=tuple(
                NamingCall(request_json=r.body.decode("utf-8"))
                for r in self.native.requests
                if r.method == "POST" and r.path == "/v1/messages"
            ),
            naming_meters=tuple(
                NamingMeter(
                    platform_user_id=r.platform_user_id,
                    managed_session_id=r.managed_session_id,
                    model=r.model,
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                )
                for r in rows
                if r.managed_session_id.startswith("thread-naming:")
                and isinstance(r.platform_user_id, str)
            ),
            callbacks_pending=sum(not task.done() for task in self.bot.message_tasks),
            queue_depth=self.bot.turn_queue.depth(),
            queue_in_flight=self.bot.turn_queue.in_flight(),
            processing_thread_ids=tuple(str(id) for id in self.bot._processing),  # pyright: ignore[reportPrivateUsage]
            pending_threads=tuple(
                PendingThread(
                    thread_id=str(thread_id), message_ids=tuple(str(m.id) for m in messages)
                )
                for thread_id, messages in self.bot._pending.items()  # pyright: ignore[reportPrivateUsage]
            ),
            errors=tuple(self.bot.ingress_errors),
            reaction_history=tuple(self.http.reaction_history),
            parsed_replies=tuple(self.bot.parsed_replies),
            message_fetches=tuple(self.http.message_fetches),
        )

    async def close(self) -> None:
        # Cancel actual gateway callback tasks; never fabricate a successful
        # completion/root or release the wire gate into an unbound provider.
        self._closed = True
        for task in self.bot.message_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.bot.message_tasks, return_exceptions=True)


class LiveIngressBinding(Observation):
    """Actual persisted host binding; never a substitute for a native root."""

    thread_id: str
    account_id: str | None
    session_id: str
    provider: Provider = "anthropic"
    binding_id: str | None = None
    lease_json: str | None = None
    active_card_id: str | None
    watermark_message_id: str | None


class ConcurrentCheckpoint(Observation):
    label: str = Field(min_length=1)
    observed_s: float = Field(ge=0)
    ingress: DiscordIngressReceipt
    live_bindings: tuple[LiveIngressBinding, ...]


class DiscordConcurrentDriver:
    """Clocked ingress observations while real callbacks remain in flight.

    The owner holds/releases its actual native wire. This driver never calls
    orchestration directly, marks a root running, supplies answers, advances a
    reducer or issues sequential replacement turns. Checkpoints expose actual
    production queues, reactions, posts and durable bindings for the owner to
    join to independent native records. They award no catalog verdict.
    """

    def __init__(self, ingress: DiscordIngress) -> None:
        self.ingress = ingress
        self.checkpoints: list[ConcurrentCheckpoint] = []

    async def checkpoint(self, label: str) -> ConcurrentCheckpoint:
        receipt = await self.ingress.receipt()
        # receipt() awaited real SQL; inspect the current replacement bundle
        # again before acquiring any additional observation connection.
        _require_runtime_bound(self.ingress.bot.runtime, self.ingress.native)
        bindings: list[LiveIngressBinding] = []
        async with self.ingress.bot.runtime.sessionmaker() as session:
            for creation in receipt.thread_creations:
                for row in await list_live_thread_sessions(
                    session,
                    tenant_id=uuid.UUID(self.ingress.scope.tenant_id),
                    platform="discord",
                    thread_id=creation.thread_id,
                ):
                    bindings.append(
                        LiveIngressBinding(
                            thread_id=row.thread_id,
                            account_id=str(row.account_id) if row.account_id is not None else None,
                            session_id=row.ma_session_id,
                            active_card_id=row.active_turn_message_id,
                            watermark_message_id=row.watermark_message_id,
                        )
                    )
            tenant_id = uuid.UUID(self.ingress.scope.tenant_id)
            for creation in receipt.thread_creations:
                slots = (
                    await session.scalars(
                        select(ProviderBindingSlot).where(
                            ProviderBindingSlot.tenant_id == tenant_id,
                            ProviderBindingSlot.platform == "discord",
                            ProviderBindingSlot.channel_id == receipt.parent_channel_id,
                            ProviderBindingSlot.thread_id == creation.thread_id,
                        )
                    )
                ).all()
                for row in slots:
                    binding = await get_binding(
                        session,
                        Slot(
                            thread=ThreadRef(
                                channel=receipt.backend_revision.channel, thread_id=row.thread_id
                            ),
                            account_id=row.account_id,
                        ),
                    )
                    if binding is None or binding.provider == "anthropic":
                        continue
                    lease = await session.scalar(
                        select(ThreadLease).where(
                            ThreadLease.tenant_id == tenant_id,
                            ThreadLease.platform == "discord",
                            ThreadLease.channel_id == receipt.parent_channel_id,
                            ThreadLease.thread_id == row.thread_id,
                            ThreadLease.account_id.is_not_distinct_from(row.account_id),
                        )
                    )
                    card_id = await session.scalar(
                        select(TurnCardIntent.message_id)
                        .where(
                            TurnCardIntent.tenant_id == tenant_id,
                            TurnCardIntent.platform == "discord",
                            TurnCardIntent.thread_id == row.thread_id,
                            TurnCardIntent.status == "posted",
                        )
                        .order_by(TurnCardIntent.created_at.desc())
                        .limit(1)
                    )
                    bindings.append(
                        LiveIngressBinding(
                            thread_id=row.thread_id,
                            account_id=row.account_id,
                            session_id=binding.native_refs["session"],
                            provider=binding.provider,
                            binding_id=binding.id,
                            lease_json=json.dumps(lease.active)
                            if lease is not None and lease.active is not None
                            else None,
                            active_card_id=card_id
                            if lease is not None and lease.active is not None
                            else None,
                            watermark_message_id=None,
                        )
                    )
        result = ConcurrentCheckpoint(
            label=label,
            observed_s=self.ingress.http.clock(),
            ingress=receipt,
            live_bindings=tuple(bindings),
        )
        self.checkpoints.append(result)
        return result

    async def wait_for(
        self,
        label: str,
        predicate: Callable[[ConcurrentCheckpoint], bool],
        *,
        timeout_s: float = 30,
    ) -> ConcurrentCheckpoint:
        """Bounded real event-loop wait; logical clocks cannot certify success."""
        async with asyncio.timeout(timeout_s):
            while True:
                result = await self.checkpoint(label)
                if result.ingress.errors:
                    raise RuntimeError("actual gateway callback failed")
                if predicate(result):
                    return result
                await asyncio.sleep(0.01)

    async def deliver_while_busy(self, payload: Mapping[str, object]) -> ConcurrentCheckpoint:
        """Require a real outstanding card/binding before the second event."""
        before = await self.checkpoint("before-concurrent-delivery")
        if (
            before.ingress.callbacks_pending == 0
            or before.ingress.queue_in_flight == 0
            or not any(binding.active_card_id for binding in before.live_bindings)
        ):
            raise ValueError("concurrent ingress requires an actual active host turn")
        self.ingress.deliver(payload)
        return before
