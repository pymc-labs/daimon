"""Offline Discord wire capture using the real adapter and discord.py objects.

Import requires the Discord adapter extra/workspace package. No login, gateway
connection, credential reads, provider extraction or headless return is involved.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import discord
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.core.turn.state import TurnState
from discord.http import HTTPClient, MultipartParameters
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from discord.types.channel import TextChannel as TextChannelPayload
    from discord.types.guild import Guild as GuildPayload
    from discord.types.message import Message as MessagePayload
    from discord.types.threads import Thread as ThreadPayload


_MESSAGE_IDS = itertools.count(
    discord.utils.time_snowflake(datetime(2026, 10, 10, tzinfo=UTC)), 1_000_000
)


class SurfaceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class DiscordMessageCapture(SurfaceModel):
    message_id: str
    channel_id: str
    order: int
    observed_s: float
    content: str
    embed_texts: tuple[str, ...]
    component_labels: tuple[str, ...]
    attachment_names: tuple[str, ...]
    payload_json: str


class DiscordPostEvent(SurfaceModel):
    evidence_id: str
    operation: Literal["send", "edit", "delete"]
    message: DiscordMessageCapture
    request_json: str


class DiscordSurfaceCapture(SurfaceModel):
    boundary: Literal["discord_adapter_posts"] = "discord_adapter_posts"
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    observed_s: float = Field(ge=0)
    events: tuple[DiscordPostEvent, ...]
    messages: tuple[DiscordMessageCapture, ...]
    post_capture_complete: bool
    text_capture_complete: bool

    @model_validator(mode="after")
    def valid_capture(self) -> DiscordSurfaceCapture:
        if self.observed_s < self.started_s:
            raise ValueError("Discord capture predates invocation")
        if any(
            not self.started_s <= event.message.observed_s <= self.observed_s
            for event in self.events
        ):
            raise ValueError("Discord post outside capture window")
        if self.text_capture_complete and not self.post_capture_complete:
            raise ValueError("complete text requires complete posts")
        return self


def _embed_texts(payload: dict[str, Any]) -> tuple[str, ...]:
    # Keep raw markdown and the payload; never substitute image pixels/alt text.
    texts: list[str] = []
    for embed in payload.get("embeds", []):
        for key in ("author", "title", "description", "fields", "footer"):
            value = embed.get(key)
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, dict):
                texts.append(
                    str(
                        cast(dict[str, Any], value).get(
                            "name", cast(dict[str, Any], value).get("text", "")
                        )
                    )
                )
            elif isinstance(value, list):
                for field in cast(list[dict[str, Any]], value):
                    texts.extend((str(field.get("name", "")), str(field.get("value", ""))))
    return tuple(text for text in texts if text)


def _component_labels(components: list[dict[str, Any]]) -> tuple[str, ...]:
    labels: list[str] = []
    for component in components:
        label = component.get("label")
        if isinstance(label, str):
            labels.append(label)
        labels.extend(_component_labels(component.get("components", [])))
    return tuple(labels)


class OfflineDiscordGateway:
    """Only owned-channel message HTTP operations exist; all others fail closed.

    discord.py performs the real serialization and creates real Message objects.
    JSON snapshots are immutable even when the adapter later mutates its embeds.
    This models an accepting Discord server, not a pixel/browser renderer.
    """

    def __init__(self, *, evidence_id: str, channel_id: int, clock: Callable[[], float]) -> None:
        self.evidence_id = evidence_id
        self.channel_id = channel_id
        self.clock = clock
        self.events: list[DiscordPostEvent] = []
        self._messages: dict[int, dict[str, Any]] = {}
        self._orders: dict[int, int] = {}
        self._next_id = next(_MESSAGE_IDS)
        self._file_bytes: dict[str, bytes] = {}
        self._failures: dict[str, Exception] = {}

    def fail_next(self, operation: Literal["send", "edit", "delete"], error: Exception) -> None:
        self._failures[operation] = error

    def _check(self, channel_id: int, operation: str) -> None:
        if channel_id != self.channel_id:
            raise ValueError("Discord capture cannot accept another channel")
        if error := self._failures.pop(operation, None):
            raise error

    def _snapshot(self, message_id: int) -> DiscordMessageCapture:
        payload = self._messages[message_id]
        return DiscordMessageCapture(
            message_id=str(message_id),
            channel_id=str(self.channel_id),
            order=self._orders[message_id],
            observed_s=self.clock(),
            content=payload["content"],
            embed_texts=_embed_texts(payload),
            component_labels=_component_labels(payload.get("components", [])),
            attachment_names=tuple(item["filename"] for item in payload["attachments"]),
            payload_json=json.dumps(payload, ensure_ascii=False),
        )

    def _record(
        self, operation: Literal["send", "edit", "delete"], message_id: int, request: dict[str, Any]
    ) -> None:
        self.events.append(
            DiscordPostEvent(
                evidence_id=f"{self.evidence_id}:post:{len(self.events)}",
                operation=operation,
                message=self._snapshot(message_id),
                request_json=json.dumps(request, ensure_ascii=False),
            )
        )

    def _request(self, params: MultipartParameters) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = params.payload
        if payload is None:
            parts = params.multipart or []
            payload = next(
                (json.loads(part["value"]) for part in parts if part["name"] == "payload_json"),
                None,
            )
        if not isinstance(payload, dict):
            raise ValueError("Discord request has no serialized payload")
        request = cast(dict[str, Any], json.loads(json.dumps(payload)))
        result = cast(dict[str, Any], json.loads(json.dumps(request)))
        if len(result.get("content") or "") > 2000:
            raise ValueError("Discord content exceeds 2000 characters")
        for file in params.files or ():
            key = f"offline://attachment/{self._next_id}/{file.filename}"
            self._file_bytes[key] = file.fp.read()
            # Preserve by-reference attachments; replace uploaded file placeholders.
            attachments = result.setdefault("attachments", [])
            index = list(params.files or ()).index(file)
            for item in attachments:
                if item.get("id") == index:
                    item.update(
                        id=self._next_id + index + 1,
                        filename=file.filename,
                        size=len(self._file_bytes[key]),
                        url=key,
                        proxy_url=key,
                    )
                    break
        return request, result

    async def send_message(self, channel_id: int, *, params: MultipartParameters) -> MessagePayload:
        self._check(channel_id, "send")
        request, response_fields = self._request(params)
        message_id = self._next_id
        self._next_id += 100
        payload: dict[str, Any] = {
            "id": str(message_id),
            "channel_id": str(channel_id),
            "type": 0,
            "content": "",
            "embeds": [],
            "attachments": [],
            "components": [],
            "author": {"id": "104", "username": "Daimon", "discriminator": "0000", "bot": True},
            "timestamp": "2026-10-10T00:00:00+00:00",
            "edited_timestamp": None,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "pinned": False,
            **response_fields,
        }
        payload["content"] = payload.get("content") or ""
        self._messages[message_id] = payload
        self._orders[message_id] = len(self._orders)
        self._record("send", message_id, request)
        return cast("MessagePayload", json.loads(json.dumps(payload)))

    async def edit_message(
        self, channel_id: int, message_id: int, *, params: MultipartParameters
    ) -> MessagePayload:
        self._check(channel_id, "edit")
        request, response_fields = self._request(params)
        payload = self._messages[message_id]
        payload.update(response_fields)
        payload["content"] = payload.get("content") or ""
        payload["edited_timestamp"] = (
            datetime(2026, 10, 10, tzinfo=UTC) + timedelta(seconds=len(self.events))
        ).isoformat()
        self._record("edit", message_id, request)
        return cast("MessagePayload", json.loads(json.dumps(payload)))

    async def delete_message(self, channel_id: int, message_id: int, **kwargs: object) -> None:
        self._check(channel_id, "delete")
        self._record("delete", message_id, {})
        del self._messages[message_id]

    async def get_message(self, channel_id: int, message_id: int) -> MessagePayload:
        self._check(channel_id, "fetch")
        return cast("MessagePayload", json.loads(json.dumps(self._messages[message_id])))

    async def get_from_cdn(self, url: str) -> bytes:
        return self._file_bytes[url]

    def surviving_messages(self) -> tuple[DiscordMessageCapture, ...]:
        return tuple(self._snapshot(message_id) for message_id in self._messages)


class _CapturedLifecycle(DiscordTurnLifecycle):
    terminal_deliveries = 0

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        self.terminal_deliveries += 1

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        self.terminal_deliveries += 1


class DiscordSurfaceHarness:
    """One owned offline turn. Feed its lifecycle to the real host turn driver.

    ``run`` ignores the invocation return. Capture identity is supplied by the
    runner and must independently match its acknowledged session/root evidence.
    Capture only covers posts routed through this transport; unrelated tool posts,
    browser pixels, permissions, webhooks and real gateway timing are not certified.
    """

    def __init__(
        self,
        *,
        evidence_id: str,
        session_id: str,
        root_turn_id: str,
        clock: Callable[[], float] = time.monotonic,
        agent_name: str = "Daimon",
        model_id: str = "claude-haiku-5-5",
        render_tables: bool = False,
        unprompted: bool = False,
        notify_on_completion: bool = False,
        cancel_view: discord.ui.View | None = None,
    ) -> None:
        self._evidence_id = evidence_id
        self._session_id = session_id
        self._root_turn_id = root_turn_id
        self._started = clock()
        DiscordSurfaceCapture(
            evidence_id=self._evidence_id,
            session_id=self._session_id,
            root_turn_id=self._root_turn_id,
            started_s=self._started,
            observed_s=self._started,
            events=(),
            messages=(),
            post_capture_complete=False,
            text_capture_complete=False,
        )
        self._clock = clock
        self._closed = False
        self._ran = False
        self._receipt: DiscordSurfaceCapture | None = None
        self.client = discord.Client(intents=discord.Intents.none())
        state = self.client._connection  # pyright: ignore[reportPrivateUsage]
        guild = discord.Guild(
            state=state,
            data=cast(
                "GuildPayload",
                {
                    "id": "100",
                    "name": "offline QA",
                    "roles": [],
                    "emojis": [],
                    "stickers": [],
                },
            ),
        )
        parent = discord.TextChannel(
            state=state,
            guild=guild,
            data=cast(
                "TextChannelPayload",
                {
                    "id": "101",
                    "name": "qa",
                    "type": 0,
                    "position": 0,
                    "permission_overwrites": [],
                },
            ),
        )
        self.thread = discord.Thread(
            state=state,
            guild=guild,
            data=cast(
                "ThreadPayload",
                {
                    "id": "102",
                    "parent_id": "101",
                    "name": "qa-thread",
                    "type": 11,
                    "owner_id": "103",
                    "message_count": 0,
                    "member_count": 0,
                    "thread_metadata": {
                        "archived": False,
                        "auto_archive_duration": 1440,
                        "archive_timestamp": "2026-10-10T00:00:00+00:00",
                        "locked": False,
                    },
                },
            ),
        )
        state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
        guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]
        guild._threads[self.thread.id] = self.thread  # pyright: ignore[reportPrivateUsage]
        self.gateway = OfflineDiscordGateway(
            evidence_id=evidence_id, channel_id=self.thread.id, clock=clock
        )
        self.client.http = cast(HTTPClient, self.gateway)
        state.http = cast(HTTPClient, self.gateway)
        self.transport = DiscordPostTransport(
            self.client,
            self.thread,
            name=agent_name,
            avatar_url=None,
            builtin=True,
            identity_enabled=False,
        )
        self.lifecycle = _CapturedLifecycle(
            send=self.transport.send,
            edit=self.transport.edit,
            delete=self.transport.delete,
            agent_name=agent_name,
            model_id=model_id,
            clock=clock,
            render_tables=render_tables,
            unprompted=unprompted,
            notify_on_completion=notify_on_completion,
            requester_id=103,
            cancel_view=cancel_view,
        )

    async def run(
        self, invoke: Callable[[DiscordTurnLifecycle], Awaitable[object]]
    ) -> DiscordSurfaceCapture:
        if self._ran:
            raise ValueError("Discord surface harness is single-use")
        self._ran = True
        try:
            await invoke(self.lifecycle)
            self._closed = self.lifecycle.terminal_deliveries == 1
        finally:
            self._receipt = self._snapshot()
        return self._receipt

    def snapshot(self) -> DiscordSurfaceCapture:
        return self._receipt if self._receipt is not None else self._snapshot()

    def _snapshot(self) -> DiscordSurfaceCapture:
        messages = self.gateway.surviving_messages()
        return DiscordSurfaceCapture(
            evidence_id=self._evidence_id,
            session_id=self._session_id,
            root_turn_id=self._root_turn_id,
            started_s=self._started,
            observed_s=self._clock(),
            events=tuple(self.gateway.events),
            messages=messages,
            post_capture_complete=self._closed,
            # Attachment pixels may contain text; no OCR/browser observation here.
            text_capture_complete=self._closed and not any(m.attachment_names for m in messages),
        )
