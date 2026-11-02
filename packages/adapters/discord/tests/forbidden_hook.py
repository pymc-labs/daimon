"""Explicit offline C4 observation over real Discord ingress and HTTP serialization."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.stores.accounts import get_account
from daimon.core.stores.tenants import get_tenant
from daimon.testing.discord_runtime_capture import require_discord_runtime_capture
from daimon.testing.discord_surface import DiscordPostEvent, OfflineDiscordGateway
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import Provider, Scope
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Host title generation precedes thread creation; it is not a provider turn write.
HOST_THREAD_NAMING_REQUEST = ("POST", "/v1/messages")

SOURCE_SHA256 = "75a44495ca1c2943c80a111e698a15bf47b440bf4b7c0acd03c5e47e1b30ab43"


class Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class OverwriteArgs(Closed):
    target: Literal["daimon_bot"]
    deny: tuple[Literal["CREATE_PUBLIC_THREADS"], ...] = Field(min_length=1, max_length=1)

    @field_validator("deny", mode="before")
    @classmethod
    def original_deny(cls, value: object) -> tuple[Literal["CREATE_PUBLIC_THREADS"], ...]:
        if not isinstance(value, list) or value != ["CREATE_PUBLIC_THREADS"]:
            raise ValueError("only original deny=[CREATE_PUBLIC_THREADS] is accepted")
        return ("CREATE_PUBLIC_THREADS",)


class OverwriteSource(Closed):
    do: Literal["admin"]
    tool: Literal["discord_overwrite"]
    args: OverwriteArgs


class CreationAttempt(Closed):
    channel_id: str
    message_id: str
    request_json: str
    observed_s: float = Field(ge=0)
    error_class: Literal["discord.Forbidden"] = "discord.Forbidden"
    status: Literal[403] = 403
    code: Literal[50013] = 50013


@dataclass(frozen=True)
class _ResponseFacts:
    status: int = 403
    reason: str = "Forbidden"


class ForbiddenGateway(OfflineDiscordGateway):
    """Owned accepting parent posts, with one genuinely forbidden thread-create edge."""

    def __init__(self, *, evidence_id: str, channel_id: int, clock: Callable[[], float]) -> None:
        super().__init__(evidence_id=evidence_id, channel_id=channel_id, clock=clock)
        self.creations: list[CreationAttempt] = []
        self.armed = False
        self.typing_channels: list[int] = []

    async def start_thread_with_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        name: str,
        auto_archive_duration: int,
        rate_limit_per_user: int | None = None,
        reason: str | None = None,
    ) -> dict[str, object]:
        if not self.armed or channel_id != self.channel_id:
            raise ValueError("unbound thread creation refused by offline fixture")
        error = discord.Forbidden(
            cast(Any, _ResponseFacts()), {"code": 50013, "message": "Missing Permissions"}
        )
        self.creations.append(
            CreationAttempt(
                channel_id=str(channel_id),
                message_id=str(message_id),
                observed_s=self.clock(),
                request_json=json.dumps(
                    {
                        "name": name,
                        "auto_archive_duration": auto_archive_duration,
                        "rate_limit_per_user": rate_limit_per_user,
                        "reason": reason,
                    }
                ),
                status=cast(Literal[403], error.status),
                code=cast(Literal[50013], error.code),
            )
        )
        raise error

    async def send_typing(self, channel_id: int) -> None:
        if channel_id != self.channel_id:
            raise ValueError("unbound typing refused by offline fixture")
        self.typing_channels.append(channel_id)

    async def add_reaction(self, channel_id: int, *args: object, **kwargs: object) -> None:
        if channel_id != self.channel_id:
            raise ValueError("unbound reaction refused by offline fixture")

    async def remove_own_reaction(self, channel_id: int, *args: object, **kwargs: object) -> None:
        if channel_id != self.channel_id:
            raise ValueError("unbound reaction refused by offline fixture")


class OverwriteReceipt(Closed):
    operation: Literal["discord_overwrite"] = "discord_overwrite"
    offline_only: Literal[True] = True
    tenant_id: str
    account_id: str
    workspace_id: str
    channel_id: str
    source: OverwriteSource
    backend_revision: ConfigRevision
    capture_started_s: float = Field(ge=0)
    configured_s: float = Field(ge=0)


class NativeRequest(Closed):
    method: str
    path: str


class ForbiddenReceipt(Closed):
    scenario_id: Literal["QA-NEW14-MISSING-THREAD-PERMISSION-COPY"] = (
        "QA-NEW14-MISSING-THREAD-PERMISSION-COPY"
    )
    source_sha256: Literal["75a44495ca1c2943c80a111e698a15bf47b440bf4b7c0acd03c5e47e1b30ab43"] = (
        SOURCE_SHA256
    )
    offline_only: Literal[True] = True
    backend: Provider
    overwrite: OverwriteReceipt
    trigger_message_id: str
    started_s: float = Field(ge=0)
    finished_s: float = Field(ge=0)
    creation: CreationAttempt
    parent_posts: tuple[DiscordPostEvent, ...]
    native_requests: tuple[NativeRequest, ...]
    queue_empty_after: bool
    processing_empty_after: bool

    @model_validator(mode="after")
    def consistent(self) -> ForbiddenReceipt:
        if (
            not self.overwrite.capture_started_s
            <= self.overwrite.configured_s
            <= self.started_s
            <= self.creation.observed_s
            <= self.finished_s
        ):
            raise ValueError("receipt clocks do not enclose actual invocation")
        if self.backend != self.overwrite.backend_revision.backend:
            raise ValueError("receipt backend differs from owned configured channel")
        if (self.creation.channel_id, self.creation.message_id) != (
            self.overwrite.channel_id,
            self.trigger_message_id,
        ):
            raise ValueError("creation differs from the owned trigger")
        if any(
            post.message.channel_id != self.overwrite.channel_id
            or not self.creation.observed_s <= post.message.observed_s <= self.finished_s
            for post in self.parent_posts
        ):
            raise ValueError("parent capture has a foreign route or time")
        return self

    @property
    def native_send_count(self) -> int:
        return sum(
            request.method not in ("GET", "HEAD")
            and (request.method, request.path) != HOST_THREAD_NAMING_REQUEST
            for request in self.native_requests
        )


class New14Checks(Closed):
    no_silent_drop: bool
    retry_text_absent: bool
    permission_text_present: bool
    zero_native_sends: bool

    @property
    def verdict(self) -> Literal["PASS", "FAIL"]:
        return "PASS" if all(self.model_dump().values()) else "FAIL"


def evaluate_new14(receipt: ForbiddenReceipt) -> New14Checks:
    texts = [
        part
        for post in receipt.parent_posts
        for part in (
            post.message.content,
            *post.message.embed_texts,
            *post.message.component_labels,
        )
        if part
    ]
    text = "\n".join(texts)
    return New14Checks(
        no_silent_drop=any(
            post.operation == "send"
            and (post.message.content.strip() or any(post.message.embed_texts))
            and post.message.observed_s - receipt.started_s <= 20
            for post in receipt.parent_posts
        ),
        retry_text_absent=re.search(r"(?i)@?mention Daimon again", text) is None,
        permission_text_present=re.search(r"(?i)create (public )?threads", text) is not None,
        zero_native_sends=receipt.native_send_count == 0,
    )


class DiscordForbiddenHook:
    """Single owned mention; no app imports this explicit offline helper."""

    def __init__(
        self,
        *,
        bot: DaimonBot,
        gateway: ForbiddenGateway,
        native: ScriptedTransport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        require_discord_runtime_capture(bot.runtime, native)
        self.bot, self.gateway, self.native, self.clock = bot, gateway, native, clock
        self.overwrite: OverwriteReceipt | None = None
        self._used = False
        self._configuration_lock = asyncio.Lock()
        self._native_bound()
        if (
            cast(object, bot.http) is not gateway
            or cast(object, bot._connection.http) is not gateway  # pyright: ignore[reportPrivateUsage]
        ):
            raise ValueError("HTTP capture is not bound to the actual Discord bot")
        if gateway.events or gateway.creations or gateway.armed or native.requests:
            raise ValueError("hook requires fresh actual capture boundaries")

    def _native_bound(self) -> None:
        require_discord_runtime_capture(self.bot.runtime, self.native)

    async def discord_overwrite_edge(
        self,
        *,
        scope: Scope,
        owned_workspace_id: str,
        owned_channel_id: str,
        source: Mapping[str, object],
        capture_started_s: float,
    ) -> OverwriteReceipt:
        async with self._configuration_lock:
            return await self._bind_overwrite(
                scope=scope,
                owned_workspace_id=owned_workspace_id,
                owned_channel_id=owned_channel_id,
                source=source,
                capture_started_s=capture_started_s,
            )

    async def _bind_overwrite(
        self,
        *,
        scope: Scope,
        owned_workspace_id: str,
        owned_channel_id: str,
        source: Mapping[str, object],
        capture_started_s: float,
    ) -> OverwriteReceipt:
        self._native_bound()
        parsed = OverwriteSource.model_validate(source)
        if (
            self.overwrite is not None
            or self._used
            or scope.is_platform
            or scope.is_legacy_host_authorized
            or not (scope.principal_id.strip() and scope.authorization_id.strip())
        ):
            raise ValueError("fresh explicit tenant authorization required")
        if owned_channel_id != str(self.gateway.channel_id):
            raise ValueError("overwrite differs from owned HTTP channel")
        parent = self.bot.get_channel(self.gateway.channel_id)
        if (
            not isinstance(parent, discord.TextChannel)
            or str(parent.guild.id) != owned_workspace_id
        ):
            raise ValueError("overwrite differs from actual owned Discord parent")
        tenant_id, account_id = uuid.UUID(scope.tenant_id), uuid.UUID(scope.account_id)
        async with self.bot.runtime.sessionmaker() as session:
            tenant = await get_tenant(session, tenant_id)
            account = await get_account(session, account_id)
            revision = await current_backend(
                session, channel_ref(tenant_id, "discord", owned_channel_id)
            )
        if (
            tenant is None
            or account is None
            or account.tenant_id != tenant_id
            or tenant.platform != "discord"
            or tenant.external_id != owned_workspace_id
            or revision is None
            or not self.bot.runtime.turn_deps.channel_backends
        ):
            raise ValueError("overwrite has no genuinely owned configured destination")
        receipt = OverwriteReceipt(
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
            workspace_id=owned_workspace_id,
            channel_id=owned_channel_id,
            source=parsed,
            backend_revision=revision,
            capture_started_s=capture_started_s,
            configured_s=self.clock(),
        )
        if receipt.capture_started_s > receipt.configured_s:
            raise ValueError("capture start is after overwrite binding")
        self._native_bound()
        self.overwrite = receipt
        self.gateway.armed = True
        return receipt

    async def mention(self, message: discord.Message) -> ForbiddenReceipt:
        self._native_bound()
        overwrite = self.overwrite
        if (
            self._used
            or overwrite is None
            or message.channel.id != self.gateway.channel_id
            or message.guild is None
            or str(message.guild.id) != overwrite.workspace_id
            or message._state is not self.bot._connection  # pyright: ignore[reportPrivateUsage]
        ):
            raise ValueError("mention requires its one owned configured capture")
        self._used = True  # reserve before the first await
        started = self.clock()
        await self.bot.on_message(message)
        self._native_bound()
        finished = self.clock()
        if len(self.gateway.creations) != 1:
            raise ValueError("real ingress did not reach genuine Forbidden thread creation once")
        async with self.bot.runtime.sessionmaker() as session:
            latest = await current_backend(
                session,
                channel_ref(uuid.UUID(overwrite.tenant_id), "discord", overwrite.channel_id),
            )
        if latest is None or latest != overwrite.backend_revision:
            raise ValueError("owned channel backend changed during observation")
        return ForbiddenReceipt(
            backend=latest.backend,
            overwrite=overwrite,
            trigger_message_id=str(message.id),
            started_s=started,
            finished_s=finished,
            creation=self.gateway.creations[0],
            parent_posts=tuple(self.gateway.events),
            native_requests=tuple(
                NativeRequest(method=r.method, path=r.path) for r in self.native.requests
            ),
            queue_empty_after=self.bot.turn_queue.depth() == 0
            and self.bot.turn_queue.in_flight() == 0,
            processing_empty_after=not self.bot._processing,  # pyright: ignore[reportPrivateUsage]
        )
