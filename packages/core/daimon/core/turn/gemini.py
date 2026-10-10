"""Explicit Gemini host codec over scoped ports; no credential/client discovery.

SDK-shaped records below are display compatibility values only. The original
neutral event and interaction usage are preserved alongside them. In particular
no Anthropic model-request span or meter is manufactured for Gemini.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
)
from daimon.core.errors import TurnError
from daimon.core.ma import REPLAY_TIMEOUT_S
from daimon.core.mux_backend import TurnBackend, TurnBackendRequest, TurnRuntime
from daimon.core.pricing import ProviderPrice
from daimon.core.turn.io import (
    TurnCodecRequest,
    TurnConnectionLost,
    TurnEvent,
    TurnStream,
    neutral_inputs,
)
from daimon.core.turn.persistence import TurnPersistence, UncertainSend
from mux.contracts.actions import InputEvent, UserMessage, UserToolResult
from mux.contracts.config import ConfigRevision
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    HistoryGapPayload,
    RequiresActionPayload,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
    UsageObservedPayload,
    UserMessagePayload,
)
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAccounting, FlashTransport
from mux.drivers.gemini.storage import Storage
from mux.drivers.gemini.transport import Transport
from mux.errors import ContinuityLost, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.store import StateStore
from pydantic import JsonValue, TypeAdapter

PROFILE = "gemini.inline_reuse"
_DISPLAY = TypeAdapter[BetaManagedAgentsSessionEvent](BetaManagedAgentsSessionEvent)
_INPUTS = TypeAdapter(list[dict[str, JsonValue]])


@dataclass(frozen=True)
class GeminiDeployment:
    config_digest: str
    config_revision: int
    account_scope_id: str
    agent: AgentSpec
    environment: EnvironmentSpec


@dataclass(frozen=True)
class GeminiJournal:
    storage: Storage
    state_store: StateStore
    deployment: GeminiDeployment | None = None


@dataclass(frozen=True)
class GeminiUsageRuntime:
    accounting: FlashAccounting
    prices: Mapping[str, ProviderPrice]
    infrastructure_usd: Decimal | None = None


@dataclass(frozen=True)
class GeminiTransportRuntime:
    transport: Transport


def compose_gemini_backend(request: TurnBackendRequest) -> TurnBackend:
    # Admission/model/durable dependencies are checked before the private
    # caller-owned transport factory, which might access a credential.
    if request.profile != PROFILE or request.model != FLASH_PRIMARY:
        raise UnsupportedCapability(("gemini_host_flash_primary",), PROFILE)
    session, runtime, config = request.session, request.runtime, request.config
    if session is None or runtime is None or config is None:
        raise UnsupportedCapability(("gemini_durable_runtime",), PROFILE)
    if (
        session.provider != "gemini"
        or session.kind != "session"
        or session.id != request.session_id
        or session.tenant_id != request.scope.tenant_id
        or session.account_id != request.scope.account_id
        or config.profile != PROFILE
        or config.channel.tenant_id != request.scope.tenant_id
    ):
        raise ScopeViolation(session.id, "Gemini runtime differs from admitted binding")
    return TurnBackend(
        backend=gemini_backend(config, request.scope, runtime, session.account_scope_id),
        session=session,
    )


def gemini_backend(
    config: ConfigRevision,
    scope: Scope,
    runtime: TurnRuntime,
    account_scope_id: str,
) -> GeminiManagedAgents:
    """Compose an explicit scoped runtime before a logical session exists."""
    if config.profile != PROFILE or config.model != FLASH_PRIMARY:
        raise UnsupportedCapability(("gemini_host_flash_primary",), PROFILE)
    if config.channel.tenant_id != scope.tenant_id:
        raise ScopeViolation(config.channel.channel_id, "Gemini runtime has a foreign channel")
    if not isinstance(runtime.journal, GeminiJournal) or not isinstance(
        runtime.usage_store, GeminiUsageRuntime
    ):
        raise UnsupportedCapability(("gemini_durable_accounting",), PROFILE)
    deployment = runtime.journal.deployment
    if (
        deployment is None
        or deployment.config_digest != config.digest
        or deployment.config_revision != config.local
        or deployment.account_scope_id != account_scope_id
    ):
        raise UnsupportedCapability(("gemini_pinned_deployment",), PROFILE)
    supplied = runtime.transport_factory(config, scope)
    if not isinstance(supplied, GeminiTransportRuntime):
        raise UnsupportedCapability(("gemini_private_transport",), PROFILE)
    transport = FlashTransport(supplied.transport, runtime.usage_store.accounting)
    return GeminiManagedAgents(
        transport,
        storage=runtime.journal.storage,
        state_store=runtime.journal.state_store,
        account_scope_id=account_scope_id,
        model_for_interaction=transport.model_for_interaction,
    )


def compose_gemini_codec(request: TurnCodecRequest) -> GeminiTurnIO:
    if request.config is None or request.model != FLASH_PRIMARY:
        raise UnsupportedCapability(("gemini_host_flash_primary",), PROFILE)
    return GeminiTurnIO(
        request.backend, request.scope, request.session, persistence=request.persistence
    )


class GeminiTurnIO:
    """One host turn, retaining the caller's durable driver/session binding."""

    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: TurnPersistence | None = None,
    ) -> None:
        if backend.capabilities().profile_id != PROFILE or session.provider != "gemini":
            raise UnsupportedCapability(("gemini_turn_codec",), PROFILE)
        if (
            session.kind != "session"
            or session.tenant_id != scope.tenant_id
            or session.account_id != scope.account_id
        ):
            raise ScopeViolation(session.id, "Gemini host session differs from authorized scope")
        self.backend, self.scope, self.session = backend, scope, session
        self.persistence = persistence
        if persistence is not None:
            persistence.check_session(scope, session)
        self.root: str | None = None
        self._ready = False
        self._streams: list[_LazyStream] = []
        self._tool_ids: dict[tuple[str | None, str], str] = {}
        self._actions: dict[str, str] = {}

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        inputs: list[InputEvent] = []
        for raw in _INPUTS.validate_python(events):
            if raw["type"] == "user.custom_tool_result":
                display_id = raw.get("custom_tool_use_id")
                if not isinstance(display_id, str) or display_id not in self._actions:
                    raise UnsupportedCapability(("unknown_function_action",), PROFILE)
                content = raw.get("content")
                if not isinstance(content, list):
                    raise ValueError("function result needs text content")
                parts = tuple(TextPart.model_validate(part) for part in content)
                inputs.append(
                    UserToolResult(
                        action_id=self._actions[display_id],
                        content=parts,
                        is_error=raw.get("is_error") is True,
                    )
                )
            else:
                # Gemini driver refuses unsupported confirmations/system input
                # before mutation. Never turn them into a new user message.
                inputs.extend(neutral_inputs([cast(BetaManagedAgentsEventParams, raw)]))
        if any(not isinstance(i, UserMessage | UserToolResult) for i in inputs):
            raise UnsupportedCapability(("gemini_host_input",), PROFILE)
        kind = (
            "send"
            if any(isinstance(i, UserMessage) for i in inputs)
            else (
                "results:"
                + ":".join(sorted(i.action_id for i in inputs if isinstance(i, UserToolResult)))
            )
        )

        async def deliver(key: str) -> SendReceipt:
            # The provider driver keeps its separate acceptance journal. The
            # host's fenced operation owns delivery and replay across workers.
            return await self.backend.events.send(
                self.scope, self.session, tuple(inputs), key=f"driver:{key}"
            )

        try:
            receipt = (
                await deliver(str(uuid4()))
                if self.persistence is None
                else await self.persistence.mutate(
                    self.session,
                    kind,
                    [i.model_dump(mode="json") for i in inputs],
                    deliver,
                    SendReceipt,
                    lambda receipt: "accepted" if receipt.status == "queued" else receipt.status,
                )
            )
        except UncertainSend as error:
            raise TurnConnectionLost(
                "Gemini delivery was already claimed; replay required"
            ) from error
        if receipt.status == "outcome_unknown":
            raise TurnConnectionLost("Gemini input acceptance is unknown; replay before recovery")
        if receipt.status not in {"queued", "processed"} or receipt.turn_id is None:
            raise ProviderError("upstream", retryable=False, native_code="gemini_send_rejected")
        self.root, self._ready = receipt.turn_id, True
        for stream in self._streams:
            stream.ready()

    async def status(self) -> str:
        session = await self.backend.sessions.retrieve(self.scope, self.session)
        return {"provisioning": "rescheduling", "requires_action": "idle"}.get(
            session.state, session.state
        )

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        if read_timeout_s <= 0:
            raise ValueError("positive stream timeout required")
        # A reconnect/restart can attach to an already accepted interaction.
        session = await self.backend.sessions.retrieve(self.scope, self.session)
        if session.binding.native_refs.get("interaction"):
            self._ready = True
            self.root = session.active_root_turn or self.root
        stream = _LazyStream(self, read_timeout_s)
        self._streams.append(stream)
        if self._ready:
            stream.ready()
        return stream

    async def usage_snapshots(self) -> dict[tuple[str, int], UsageObservation]:
        result: dict[tuple[str, int], UsageObservation] = {}
        cursor = None
        seen: set[str] = set()
        while True:
            page = await self.backend.usage.list(
                self.scope, self.session, page=PageRequest(cursor=cursor, limit=100)
            )
            for usage in page.data:
                if usage.session != self.session or usage.model is None:
                    raise ProviderError("upstream", retryable=False, native_code="foreign_usage")
                if usage.model.provider != "gemini":
                    raise ProviderError("upstream", retryable=False, native_code="foreign_meter")
                result[usage.id, usage.revision] = usage
            cursor = page.next_cursor
            if cursor is None:
                return result
            if cursor in seen:
                raise ProviderError("upstream", retryable=False, native_code="usage_cursor_cycle")
            seen.add(cursor)

    async def _history(self) -> list[Event]:
        records: list[Event] = []
        cursor = None
        seen: set[str] = set()
        while True:
            page = await self.backend.events.list(
                self.scope, self.session, page=PageRequest(cursor=cursor, limit=100)
            )
            for event in page.data:
                if event.session_id != self.session.id or event.native.provider != "gemini":
                    raise ProviderError("upstream", retryable=False, native_code="foreign_event")
                if event.authority != "preview":
                    records.append(event)
            cursor = page.next_cursor
            if cursor is None:
                return records
            if cursor in seen:
                raise ProviderError("upstream", retryable=False, native_code="event_cursor_cycle")
            seen.add(cursor)

    def frame(self, event: Event, usages: dict[tuple[str, int], UsageObservation]) -> TurnEvent:
        if event.session_id != self.session.id or event.native.provider != "gemini":
            raise ProviderError("upstream", retryable=False, native_code="foreign_event")
        if event.authority == "preview":
            raise ValueError("preview cannot enter Gemini authoritative codec")
        payload = event.typed_payload()
        display: dict[str, object] = {
            "id": event.id,
            "processed_at": event.occurred_at or event.observed_at,
        }
        usage: UsageObservation | None = None
        if isinstance(payload, AgentMessagePayload | UserMessagePayload):
            if any(not isinstance(part, TextPart) for part in payload.content):
                raise UnsupportedCapability(("gemini_native_message_display",), PROFILE)
            display.update(type=event.type, content=[p.model_dump() for p in payload.content])
        elif isinstance(payload, ToolUsePayload):
            self._tool_ids[event.turn_id, payload.call_id] = event.id
            if payload.executor == "host":
                self._actions[event.id] = payload.call_id
            display.update(
                type="agent.custom_tool_use" if payload.executor == "host" else "agent.tool_use",
                name=payload.tool_name,
                input=dict(payload.input),
            )
        elif isinstance(payload, ToolResultPayload):
            paired = self._tool_ids.get((event.turn_id, payload.call_id))
            if paired is None:
                raise ProviderError("upstream", retryable=False, native_code="orphan_tool_result")
            if any(not isinstance(part, TextPart) for part in payload.content):
                raise UnsupportedCapability(("gemini_native_tool_result_display",), PROFILE)
            display.update(
                type="agent.tool_result",
                tool_use_id=paired,
                content=[p.model_dump() for p in payload.content],
                is_error=payload.is_error,
            )
        elif isinstance(payload, TurnEndedPayload):
            if payload.root_turn_id != event.turn_id:
                raise ProviderError("upstream", retryable=False, native_code="foreign_root_end")
            if payload.outcome in {"completed", "interrupted"}:
                display.update(type="session.status_idle", stop_reason={"type": "end_turn"})
            else:
                display.update(type="session.status_terminated")
        elif isinstance(payload, RequiresActionPayload):
            ids = [self._tool_ids.get((event.turn_id, a.call_id or a.id)) for a in payload.actions]
            if any(id_ is None for id_ in ids):
                raise UnsupportedCapability(("gemini_native_required_action",), PROFILE)
            display.update(
                type="session.status_idle",
                stop_reason={"type": "requires_action", "event_ids": ids},
            )
        elif isinstance(payload, HistoryGapPayload):
            if not payload.recoverable:
                raise ContinuityLost(
                    self.session.id, ("Gemini stream history has an unrecoverable gap.",)
                )
            return TurnEvent(native=None, normalized=event)
        else:
            if isinstance(payload, UsageObservedPayload):
                usage = usages.get((payload.observation_id, payload.revision))
                if usage is None or usage.turn_id != event.turn_id:
                    raise ProviderError("upstream", retryable=False, native_code="missing_usage")
            return TurnEvent(native=None, normalized=event, usage=usage)
        return TurnEvent(_DISPLAY.validate_python(display), event, usage)

    def check_terminal(self, event: Event, *, replay: bool = False) -> None:
        if event.authority not in {"record", "reconciled"} or event.turn_id != self.root:
            return
        payload = event.typed_payload()
        if not isinstance(payload, TurnEndedPayload) or payload.root_turn_id != self.root:
            return
        if payload.outcome in {"errored", "terminated"}:
            raise ProviderError("upstream", retryable=False, native_code="host_root_failed")
        if replay and payload.outcome == "interrupted":
            from daimon.core.turn.driver import InterruptedDuringRecovery

            # Preserve saved cancellation across the SDK-only replay edge.
            raise InterruptedDuringRecovery(phase="replay")

    async def replay_turn_events(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S, reconcile: bool = True
    ) -> list[TurnEvent]:
        try:
            async with asyncio.timeout(timeout_s):
                if reconcile:
                    await self.backend.events.reconcile(self.scope, self.session)
                records = await self._history()
                usages = await self.usage_snapshots()
                if self.root is None:
                    self.root = next((e.turn_id for e in reversed(records) if e.turn_id), None)
                if self.persistence is not None and records:
                    final = records[-1]
                    await self.persistence.record_many(
                        self.session,
                        records,
                        cursor=final.native.cursor or final.native.event_id or final.id,
                    )
                for event in records:
                    self.check_terminal(event)
                return [self.frame(event, usages) for event in records]
        except TimeoutError as error:
            raise TurnError(kind="upstream", message="Gemini replay deadline expired") from error

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        # N8's host dispatch separately requires replay_usage before send and
        # consumes it after replay and every pump exit, including corrections.
        frames = await self.replay_turn_events(timeout_s=timeout_s)
        for frame in frames:
            if frame.normalized is not None:
                self.check_terminal(frame.normalized, replay=True)
        return [
            _DISPLAY.validate_python(frame.native.model_dump())
            for frame in frames
            if frame.native is not None
        ]

    async def replay_usage(self) -> Sequence[UsageObservation]:
        async with asyncio.timeout(REPLAY_TIMEOUT_S):
            await self.backend.usage.reconcile(self.scope, self.session)
            if self.root is None:
                records = await self._history()
                self.root = next((e.turn_id for e in reversed(records) if e.turn_id), None)
            observations = await self.usage_snapshots()
            latest: dict[str, UsageObservation] = {}
            for observation in observations.values():
                if self.root is None or observation.turn_id != self.root:
                    continue
                prior = latest.get(observation.id)
                if prior is None or observation.revision > prior.revision:
                    latest[observation.id] = observation
            return tuple(latest.values())

    def forget_stream(self, stream: _LazyStream) -> None:
        if stream in self._streams:
            self._streams.remove(stream)

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        try:
            return await self._interrupt(timeout_s=timeout_s)
        except TimeoutError as error:
            raise TurnError(
                kind="interrupt_timeout", message="Gemini cancellation deadline expired"
            ) from error

    async def _interrupt(self, *, timeout_s: float) -> StopObservation:
        deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
        async with asyncio.timeout(timeout_s):
            projection = await self.backend.events.reconcile(self.scope, self.session)
            root = projection.active_root_turn or self.root
            if root is None:
                records = await self._history()
                root = next((event.turn_id for event in reversed(records) if event.turn_id), None)
            if root is None:
                raise UnsupportedCapability(("unknown_cancel_root",), PROFILE)

            async def deliver(key: str) -> CancelReceipt:
                receipt = await self.backend.events.cancel(
                    self.scope, self.session, turn_id=root, key=f"driver:{key}"
                )
                return receipt.model_copy(update={"operation_id": key})

            try:
                receipt = (
                    await deliver(str(uuid4()))
                    if self.persistence is None
                    else await self.persistence.mutate(
                        self.session,
                        "cancel",
                        {"turn_id": root},
                        deliver,
                        CancelReceipt,
                        lambda r: (
                            "outcome_unknown" if r.status == "outcome_unknown" else "accepted"
                        ),
                    )
                )
            except UncertainSend as error:
                raise TurnConnectionLost("Gemini cancel already claimed; observe stop") from error
            if receipt.session != self.session or receipt.turn_id != root:
                raise ProviderError(
                    "upstream", retryable=False, native_code="foreign_cancel_receipt"
                )
            while True:
                stopped = await self.backend.events.wait_stopped(
                    self.scope, receipt, deadline=deadline
                )
                if stopped.receipt_operation_id != receipt.operation_id:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="foreign_stop_observation"
                    )
                if stopped.stopped:
                    if self.persistence is not None:
                        records = await self._history()
                        if records:
                            final = records[-1]
                            await self.persistence.record_many(
                                self.session,
                                records,
                                cursor=final.native.cursor or final.native.event_id or final.id,
                            )
                        await self.persistence.stopped(receipt.operation_id, self.session)
                    return stopped
                if datetime.now(UTC) >= deadline:
                    raise TurnError(
                        kind="interrupt_timeout", message="Gemini cancel was not observed"
                    )
                await asyncio.sleep(
                    min(0.05, max(0, (deadline - datetime.now(UTC)).total_seconds()))
                )

    async def archive(self) -> None:
        # Native unsupported archive remains typed; never send an Anthropic call.
        await self.backend.sessions.archive(self.scope, self.session, key=str(uuid4()))


class _LazyStream(AsyncIterator[TurnEvent]):
    def __init__(self, io: GeminiTurnIO, timeout_s: float) -> None:
        self.io, self.timeout_s = io, timeout_s
        self._gate = asyncio.Event()
        self._closed = False
        self._source: AsyncIterator[Event] | None = None
        self._buffer: deque[TurnEvent] = deque()
        self._seen: set[str] = set()
        self._root: str | None = None
        self._ended = False
        self._reader: asyncio.Task[object] | None = None

    def ready(self) -> None:
        self._gate.set()

    def __aiter__(self) -> _LazyStream:
        return self

    async def __anext__(self) -> TurnEvent:
        if self._closed:
            raise StopAsyncIteration
        if self._reader is not None:
            raise RuntimeError("concurrent Gemini stream reads are unsupported")
        self._reader = asyncio.current_task()
        try:
            async with asyncio.timeout(self.timeout_s):
                await self._gate.wait()
                if self._closed:
                    raise StopAsyncIteration
                if self._source is None:
                    self._root = self.io.root
                    self._source = await self.io.backend.events.open_stream(
                        self.io.scope, self.io.session
                    )
                    if self._closed:
                        source, self._source = self._source, None
                        close = getattr(source, "aclose", None)
                        if close is not None:
                            await close()
                        raise StopAsyncIteration
                    frames = await self.io.replay_turn_events(reconcile=False)
                    if self._root is None and frames:
                        self._root = self.io.root
                    self._buffer.extend(
                        f for f in frames if f.normalized and f.normalized.turn_id == self._root
                    )
                while True:
                    if self._buffer:
                        frame = self._buffer.popleft()
                    else:
                        if self._ended:
                            await self.close()
                            raise StopAsyncIteration
                        try:
                            event = await self._source.__anext__()
                        except StopAsyncIteration:
                            raise TurnConnectionLost(
                                "Gemini stream ended without an authoritative root outcome"
                            ) from None
                        if event.authority == "preview":
                            continue
                        frame = self.io.frame(event, await self.io.usage_snapshots())
                    normalized = frame.normalized
                    if normalized is None or normalized.id in self._seen:
                        continue
                    if normalized.turn_id != self._root:
                        raise ProviderError(
                            "upstream", retryable=False, native_code="foreign_stream_root"
                        )
                    self._seen.add(normalized.id)
                    if self.io.persistence is not None:
                        await self.io.persistence.record(self.io.session, normalized)
                    self.io.check_terminal(normalized)
                    self._ended |= normalized.type == "session.turn_ended"
                    return frame
        except BaseException:
            await self.close()
            raise
        finally:
            self._reader = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate.set()
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await reader
        source, self._source = self._source, None
        if source is not None:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
        self.io.forget_stream(self)
