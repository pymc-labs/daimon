"""Turn I/O and the temporary native compatibility edge.

Neutral ports never return SDK objects. Existing reducers and lifecycle hooks
still receive their original native records here during M0; this edge owns
that decoding and must disappear when those consumers adopt neutral events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import uuid4

import anthropic
import httpx
from anthropic._models import construct_type
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsStreamSessionEvents,
)
from daimon.core.config import load_turn_settings
from daimon.core.errors import TurnError
from daimon.core.ma import REPLAY_TIMEOUT_S, replay_events, send_interrupt_and_wait
from mux.contracts.actions import InputEvent, NativeInput, UserMessage, UserToolConfirmation
from mux.contracts.events import ContentPart, Event, ImagePart, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import StopObservation
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.transport import LegacyTurnTransport
from mux.drivers.anthropic.turn import EventHistoryWalk
from mux.drivers.anthropic.usage import observation_from_event
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
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
    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]: ...
    async def interrupt(self, *, timeout_s: float) -> StopObservation | None: ...
    async def archive(self) -> None: ...


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
        self._client = client
        self._session_id = session_id

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self._transport.send(events)

    async def status(self) -> str:
        return await self._transport.status()

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return _LegacyStream(await self._transport.open_stream(read_timeout_s=read_timeout_s))

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        return await replay_events(self._client, session_id=self._session_id, timeout_s=timeout_s)

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        await send_interrupt_and_wait(
            self._client, session_id=self._session_id, timeout_s=timeout_s
        )
        return None

    async def archive(self) -> None:
        await self._transport.archive()


class _MuxStream(AsyncIterator[TurnEvent]):
    def __init__(
        self,
        source: AsyncIterator[Event],
        session: ResourceRef,
        on_record: Callable[[Event], None],
    ) -> None:
        self._source = source
        self._session = session
        self._on_record = on_record

    def __aiter__(self) -> _MuxStream:
        return self

    async def __anext__(self) -> TurnEvent:
        while True:
            event = await _native_error_edge(self._source.__anext__())
            if event.authority == "preview":
                # The existing host consumes finalized messages only. Preview
                # authority cannot bill, mutate durable history or end a turn.
                continue
            self._on_record(event)
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
        if session.kind != "session" or (
            not (scope.is_platform or scope.is_legacy_host_authorized)
            and (session.tenant_id != scope.tenant_id or session.account_id != scope.account_id)
        ):
            raise ScopeViolation(session.id, "turn session differs from the authorized scope")
        self._backend = backend
        self._scope = scope
        self._session = session
        self._turn_id: str | None = None
        self._operation_id = str(uuid4())

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

    async def archive(self) -> None:
        await _native_error_edge(
            self._backend.sessions.archive(self._scope, self._session, key=str(uuid4()))
        )

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        # The injected Events implementation owns its read timeout. The default
        # Anthropic composition receives this value when the host builds it.
        source = await _native_error_edge(
            self._backend.events.open_stream(self._scope, self._session)
        )
        return _MuxStream(source, self._session, self._remember_root)

    def _remember_root(self, event: Event) -> None:
        if event.turn_id is not None:
            self._turn_id = event.turn_id

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        history = self._backend.extension(
            EventHistoryWalk, namespace="anthropic.event_history", version=1
        )

        async def walk() -> list[BetaManagedAgentsSessionEvent]:
            events: list[BetaManagedAgentsSessionEvent] = []
            source = history.walk(self._scope, self._session)
            try:
                while True:
                    try:
                        event = await _native_error_edge(source.__anext__())
                    except StopAsyncIteration:
                        break
                    if event.authority == "preview":
                        continue
                    self._remember_root(event)
                    record = event.native.record
                    if event.native.provider != "anthropic" or not isinstance(record, dict):
                        raise UnsupportedCapability(("legacy_event_codec",), "daimon.turn")
                    events.append(
                        cast(
                            BetaManagedAgentsSessionEvent,
                            construct_type(type_=BetaManagedAgentsSessionEvent, value=record),
                        )
                    )
            finally:
                close = getattr(source, "aclose", None)
                if callable(close):
                    await cast(Callable[[], Awaitable[None]], close)()
            return events

        try:
            return await asyncio.wait_for(walk(), timeout=timeout_s)
        except TimeoutError as error:
            raise TurnError(
                kind="upstream", message=f"MA event replay did not complete within {timeout_s}s"
            ) from error

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        receipt = await _native_error_edge(
            self._backend.events.cancel(
                self._scope,
                self._session,
                turn_id=self._turn_id or self._operation_id,
                key=str(uuid4()),
            )
        )
        if receipt.session != self._session:
            raise ProviderError("upstream", retryable=False, native_code="foreign_cancel_receipt")
        deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
        stopped = await _native_error_edge(
            self._backend.events.wait_stopped(
                self._scope,
                receipt,
                deadline=deadline,
            )
        )
        if stopped.receipt_operation_id != receipt.operation_id:
            raise ProviderError("upstream", retryable=False, native_code="foreign_stop_observation")
        if not stopped.stopped:
            raise TurnError(
                kind="interrupt_timeout",
                message=(
                    f"MA did not acknowledge interrupt within {timeout_s}s"
                    if datetime.now(UTC) >= deadline
                    else f"MA SSE stream closed without terminal idle (timeout {timeout_s}s)"
                ),
            )
        return stopped


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


def turn_io(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    *,
    path: Literal["legacy", "mux"] | None = None,
    backend: ManagedAgents | None = None,
    scope: Scope | None = None,
    session_ref: ResourceRef | None = None,
    read_timeout_s: float = 120.0,
) -> TurnIO:
    """Bind every turn helper to the same authorized session as its driver."""
    selected = path if path is not None else load_turn_settings().path
    if selected == "legacy":
        return LegacyTurnIO(client, session_id)
    if scope is None:
        raise ScopeViolation(session_id, "mux turns require the caller's authorized scope")
    if backend is None:
        return default_mux_turn_io(client, scope, session_id, read_timeout_s=read_timeout_s)
    if session_ref is None or session_ref.id != session_id:
        raise ScopeViolation(session_id, "an injected backend requires the bound session ref")
    return MuxTurnIO(backend, scope, session_ref)
