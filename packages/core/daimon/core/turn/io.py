"""Turn I/O and the temporary native compatibility edge.

Neutral ports never return SDK objects. Existing reducers and lifecycle hooks
still receive their original native records here during M0; this edge owns
that decoding and must disappear when those consumers adopt neutral events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

import anthropic
import httpx
from anthropic._models import construct_type
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsStreamSessionEvents,
)
from mux.contracts.actions import InputEvent, NativeInput, UserMessage, UserToolConfirmation
from mux.contracts.events import ContentPart, Event, ImagePart, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.transport import LegacyTurnTransport
from mux.drivers.anthropic.usage import observation_from_event
from mux.errors import ProviderError, UnsupportedCapability
from pydantic import JsonValue, TypeAdapter

_JSON_INPUTS = TypeAdapter(list[dict[str, JsonValue]])


@dataclass(frozen=True)
class TurnEvent:
    native: BetaManagedAgentsStreamSessionEvents
    normalized: Event | None = None
    usage: UsageObservation | None = None


class TurnStream(Protocol):
    def __aiter__(self) -> AsyncIterator[TurnEvent]: ...
    async def __anext__(self) -> TurnEvent: ...
    async def close(self) -> None: ...


class TurnIO(Protocol):
    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None: ...
    async def status(self) -> str: ...
    async def open_stream(self, *, read_timeout_s: float) -> TurnStream: ...


class TurnConnectionLost(Exception):
    """A neutral connection failure; reconnect by replay, never resend input."""


async def _native_error_edge[T](call: Awaitable[T]) -> T:
    """Preserve current host error/retry behavior for the Anthropic M0 edge."""
    try:
        return await call
    except ProviderError as error:
        if isinstance(error.__cause__, anthropic.APIError | httpx.HTTPError):
            raise error.__cause__ from error
        if error.category == "transient_network":
            raise TurnConnectionLost(str(error)) from error
        raise


class _LegacyStream(AsyncIterator[TurnEvent]):
    def __init__(self, source: anthropic.AsyncStream[BetaManagedAgentsStreamSessionEvents]) -> None:
        self._source = source
        self._iterator = source.__aiter__()

    def __aiter__(self) -> _LegacyStream:
        return self

    async def __anext__(self) -> TurnEvent:
        return TurnEvent(await self._iterator.__anext__())

    async def close(self) -> None:
        await self._source.close()


class LegacyTurnIO:
    def __init__(self, client: anthropic.AsyncAnthropic, session_id: str) -> None:
        self._transport = LegacyTurnTransport(client, session_id)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self._transport.send(events)

    async def status(self) -> str:
        return await self._transport.status()

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return _LegacyStream(await self._transport.open_stream(read_timeout_s=read_timeout_s))


class _MuxStream(AsyncIterator[TurnEvent]):
    def __init__(self, source: AsyncIterator[Event], session: ResourceRef) -> None:
        self._source = source
        self._session = session

    def __aiter__(self) -> _MuxStream:
        return self

    async def __anext__(self) -> TurnEvent:
        while True:
            event = await _native_error_edge(self._source.__anext__())
            if event.authority == "preview":
                # The existing host consumes finalized messages only. Preview
                # authority cannot bill, mutate durable history or end a turn.
                continue
            record = event.native.record
            if event.native.provider != "anthropic" or not isinstance(record, dict):
                raise UnsupportedCapability(("legacy_event_codec",), "daimon.turn")
            native = cast(
                BetaManagedAgentsStreamSessionEvents,
                construct_type(type_=BetaManagedAgentsStreamSessionEvents, value=record),
            )
            usage = (
                observation_from_event(
                    record,
                    self._session,
                    observed_at=event.occurred_at or event.observed_at,
                    turn_id=event.turn_id,
                    thread_id=event.thread_id,
                )
                if event.type == "usage.observed"
                else None
            )
            return TurnEvent(native, event, usage)

    async def close(self) -> None:
        close = getattr(self._source, "aclose", None)
        if callable(close):
            await cast(Callable[[], Awaitable[None]], close)()


def _content(raw: JsonValue) -> tuple[ContentPart, ...]:
    if not isinstance(raw, list):
        raise ValueError("message content must be a list")
    parts: list[ContentPart] = []
    for block in raw:
        if not isinstance(block, dict):
            raise ValueError("message content must contain objects")
        if block.get("type") == "text":
            parts.append(TextPart.model_validate(block))
        elif block.get("type") == "image":
            source = block.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                raise UnsupportedCapability(("artifact_input",), "daimon.turn")
            parts.append(
                ImagePart.model_validate(
                    {"media_type": source.get("media_type"), "data_base64": source.get("data")}
                )
            )
        else:
            raise UnsupportedCapability(("native_content_input",), "daimon.turn")
    return tuple(parts)


def neutral_inputs(events: Sequence[BetaManagedAgentsEventParams]) -> tuple[InputEvent, ...]:
    """Translate only the typed host inputs this turn driver produces."""
    inputs: list[InputEvent] = []
    for event in _JSON_INPUTS.validate_python(events):
        match event["type"]:
            case "user.message":
                inputs.append(UserMessage(content=_content(event["content"])))
            case "user.tool_confirmation":
                inputs.append(
                    UserToolConfirmation.model_validate(
                        {
                            "action_id": event["tool_use_id"],
                            "decision": event["result"],
                            "deny_message": event.get("deny_message"),
                        }
                    )
                )
            case "system.message":
                inputs.append(
                    NativeInput(
                        extension=ExtensionConfig(
                            namespace="anthropic.session_system_message",
                            version=1,
                            value={"content": event["content"]},
                        )
                    )
                )
            case _:
                raise UnsupportedCapability(("turn_input",), "daimon.turn")
    return tuple(inputs)


class MuxTurnIO:
    def __init__(self, backend: ManagedAgents, scope: Scope, session: ResourceRef) -> None:
        self._backend = backend
        self._scope = scope
        self._session = session

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        receipt = await _native_error_edge(
            self._backend.events.send(
                self._scope, self._session, neutral_inputs(events), key=str(uuid4())
            )
        )
        if receipt.status == "outcome_unknown":
            # Never acknowledge acceptance or repeat this batch. The existing
            # reconnect machinery will replay before attaching another stream.
            raise TurnConnectionLost("input acknowledgement was lost; its outcome is unknown")
        if receipt.status not in {"processed", "queued"}:
            raise ProviderError("upstream", retryable=False, native_code="send_rejected")

    async def status(self) -> str:
        session = await _native_error_edge(
            self._backend.sessions.retrieve(self._scope, self._session)
        )
        return {"provisioning": "rescheduling", "requires_action": "idle"}.get(
            session.state, session.state
        )

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        # The injected Events implementation owns its read timeout. The default
        # Anthropic composition receives this value when the host builds it.
        source = await _native_error_edge(
            self._backend.events.open_stream(self._scope, self._session)
        )
        return _MuxStream(source, self._session)


def default_mux_turn_io(
    client: anthropic.AsyncAnthropic,
    scope: Scope,
    session_id: str,
    *,
    read_timeout_s: float,
) -> MuxTurnIO:
    """Compose ports around the existing client and its authorized session.

    N5 owns registration of Sessions in the composable factory. No lifecycle
    implementation, credential lookup or provider I/O belongs here.
    """
    from mux.drivers.anthropic import AnthropicManagedAgents
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.turn import AnthropicEvents

    workspace = str(uuid4())
    authorization = ResourceAuthorization(scope, frozenset({("session", session_id)}))
    backend = AnthropicManagedAgents(
        client,
        account_scope_id=workspace,
        authorization=authorization,
        events=AnthropicEvents(
            client, workspace, authorization, stream_read_timeout_s=read_timeout_s
        ),
    )
    ref = ResourceRef(
        id=session_id,
        kind="session",
        provider="anthropic",
        account_scope_id=workspace,
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    return MuxTurnIO(backend, scope, ref)
