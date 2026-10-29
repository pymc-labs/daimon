"""Turn I/O and the temporary native compatibility edge.

Neutral ports never return SDK objects. Existing reducers and lifecycle hooks
still receive their original native records here during M0; this edge owns
that decoding and must disappear when those consumers adopt neutral events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
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
from daimon.core.mux_backend import TurnBackendRequest, TurnRuntime, turn_backend
from daimon.core.turn.persistence import TurnPersistence, UncertainSend
from daimon.core.turn.provider_actions import BoundProviderActions, ProviderActionApproval
from mux.contracts.actions import InputEvent, NativeInput, UserMessage, UserToolConfirmation
from mux.contracts.config import ConfigRevision
from mux.contracts.events import ContentPart, Event, ImagePart, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, Operation, SendReceipt, StopObservation
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic import (
    EventHistoryWalk,
    EventNormalizer,
    LegacyTurnTransport,
    object_json,
    observation_from_event,
)
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.operations import request_digest
from pydantic import JsonValue, TypeAdapter

_JSON_INPUTS = TypeAdapter(list[dict[str, JsonValue]])


@dataclass(frozen=True)
class TurnEvent:
    native: BetaManagedAgentsStreamSessionEvents | None = None
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


@dataclass(frozen=True)
class TurnCodecRequest:
    backend: ManagedAgents
    scope: Scope
    session: ResourceRef
    read_timeout_s: float = 120.0
    config: ConfigRevision | None = None
    runtime: TurnRuntime | None = None
    persistence: TurnPersistence | None = None
    provider_actions: BoundProviderActions | None = None

    @property
    def model(self) -> str | None:
        return self.config.model if self.config is not None else None


TurnCodecFactory = Callable[[TurnCodecRequest], TurnIO]
_TURN_CODECS: dict[str, TurnCodecFactory] = {}


def register_turn_codec(profile: str, factory: TurnCodecFactory, *, fallback: bool = False) -> None:
    """Provider modules register their host compatibility codec at startup."""
    if profile in _TURN_CODECS:
        if fallback:
            return
        raise ValueError(f"turn codec already registered: {profile}")
    _TURN_CODECS[profile] = factory


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
    def __init__(
        self, client: anthropic.AsyncAnthropic, session_id: str, *, scope: Scope | None = None
    ) -> None:
        self._transport = LegacyTurnTransport(client, session_id, scope=scope)
        self._client = client
        self._session_id = session_id
        self._scope = scope

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self._transport.send(events)

    async def status(self) -> str:
        return await self._transport.status()

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return _LegacyStream(await self._transport.open_stream(read_timeout_s=read_timeout_s))

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        return await replay_events(
            self._client, session_id=self._session_id, timeout_s=timeout_s, scope=self._scope
        )

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        await send_interrupt_and_wait(
            self._client, session_id=self._session_id, timeout_s=timeout_s, scope=self._scope
        )
        return None

    async def archive(self) -> None:
        await self._transport.archive()


class _TrackedLegacyStream(_LegacyStream):
    def __init__(
        self,
        source: anthropic.AsyncStream[BetaManagedAgentsStreamSessionEvents],
        persistence: TurnPersistence,
        session: ResourceRef,
    ) -> None:
        super().__init__(source)
        self._persistence = persistence
        self._session = session
        self._normalizer = EventNormalizer(session)

    async def __anext__(self) -> TurnEvent:
        try:
            event = await super().__anext__()
        except (StopAsyncIteration, httpx.HTTPError, anthropic.APIConnectionError):
            await self._persistence.gap(self._session)
            raise
        if event.native is None:
            raise TurnConnectionLost("native recovery stream has no SDK event")
        normalized = self._normalizer.normalize(
            object_json(event.native.model_dump(mode="json")), observed_at=datetime.now(UTC)
        )
        await self._persistence.record(self._session, normalized)
        return event


class TrackedLegacyTurnIO(LegacyTurnIO):
    """Keep main's native recovery HTTP while retaining the mux turn's fence."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        session_id: str,
        *,
        scope: Scope,
        persistence: TurnPersistence,
    ) -> None:
        super().__init__(client, session_id, scope=scope)
        self._persistence = persistence
        self._session = ResourceRef(
            id=session_id,
            kind="session",
            provider="anthropic",
            account_scope_id="native-confirmation-recovery",
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )
        persistence.check_session(scope, self._session)

    async def _execute(
        self,
        kind: str,
        request: JsonValue,
        call: Callable[[], Awaitable[None]],
        *,
        processed: bool = False,
    ) -> None:
        async def deliver(key: str) -> SendReceipt:
            await call()
            return SendReceipt(
                operation_id=key, status="processed" if processed else "queued", input_ids=()
            )

        try:
            await self._persistence.mutate(
                self._session,
                kind,
                request,
                deliver,
                SendReceipt,
                lambda receipt: "processed" if receipt.status == "processed" else "accepted",
            )
        except UncertainSend as error:
            raise TurnConnectionLost(str(error)) from error

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        request: JsonValue
        if any(event["type"] == "user.interrupt" for event in events):
            kind = "recovery-interrupt"
            request = [event for event in _JSON_INPUTS.validate_python(events)]
        else:
            inputs = neutral_inputs(events)
            kind = _send_kind(inputs)
            request = [event.model_dump(mode="json") for event in inputs]
        await self._execute(kind, request, lambda: self._transport.send(events))

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return _TrackedLegacyStream(
            await self._transport.open_stream(read_timeout_s=read_timeout_s),
            self._persistence,
            self._session,
        )

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        events = await super().replay(timeout_s=timeout_s)
        normalizer = EventNormalizer(self._session)
        for event in events:
            normalized = normalizer.normalize(
                object_json(event.model_dump(mode="json")), observed_at=datetime.now(UTC)
            )
            await self._persistence.record(self._session, normalized)
        return events

    async def interrupt(self, *, timeout_s: float) -> None:
        await self._execute(
            "recovery-stop",
            {},
            lambda: send_interrupt_and_wait(
                self._client, session_id=self._session_id, timeout_s=timeout_s, scope=self._scope
            ),
            processed=True,
        )

    async def archive(self) -> None:
        async def deliver(key: str) -> Operation:
            await self._transport.archive()
            observed_at = datetime.now(UTC)
            return Operation(
                id=key,
                key=key,
                request_digest=request_digest({}),
                status="processed",
                resource=self._session,
                created_at=observed_at,
                updated_at=observed_at,
            )

        try:
            await self._persistence.mutate(
                self._session, "archive", {}, deliver, Operation, lambda receipt: receipt.status
            )
        except UncertainSend as error:
            raise TurnConnectionLost(str(error)) from error


class _MuxStream(AsyncIterator[TurnEvent]):
    def __init__(
        self,
        source: AsyncIterator[Event],
        session: ResourceRef,
        on_record: Callable[[Event], Awaitable[None]],
        on_gap: Callable[[], Awaitable[None]],
    ) -> None:
        self._source = source
        self._session = session
        self._on_record = on_record
        self._on_gap = on_gap

    def __aiter__(self) -> _MuxStream:
        return self

    async def __anext__(self) -> TurnEvent:
        while True:
            try:
                event = await _native_error_edge(self._source.__anext__())
            except (
                StopAsyncIteration,
                TurnConnectionLost,
                httpx.HTTPError,
                anthropic.APIConnectionError,
            ):
                await self._on_gap()
                raise
            await self._on_record(event)
            if event.authority == "preview":
                # The existing host consumes finalized messages only. Preview
                # authority cannot bill or end a turn. A journal may keep it
                # in its separate preview namespace.
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


def _input_id(event: Event) -> str | None:
    """The native id of a recorded user.message, the only input that opens a turn."""
    if event.authority == "preview" or event.native.event_type != "user.message":
        return None
    return event.native.event_id


def _send_kind(inputs: Sequence[InputEvent]) -> str:
    if any(isinstance(event, UserMessage) for event in inputs):
        return "send"
    action_ids: JsonValue = [
        action_id
        for action_id in sorted(
            event.action_id for event in inputs if isinstance(event, UserToolConfirmation)
        )
    ]
    return "actions:" + request_digest(action_ids)


class _SubscribedCancel(Protocol):
    async def wait_stopped_before_cancel(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        cancel: Callable[[], Awaitable[CancelReceipt]],
        deadline: datetime,
    ) -> tuple[CancelReceipt, StopObservation]: ...


class MuxTurnIO:
    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: TurnPersistence | None = None,
    ) -> None:
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
        self._persistence = persistence
        # This turn's own input (F12): the receipt's native ids, or, after this
        # process posted it and lost the answer, proof found on replay. A
        # receipt restored without posting has nothing to compare against.
        self._own_inputs: tuple[str, ...] = ()
        self._unconfirmed = False
        # The journal's inputs before the first replay after an unconfirmed
        # send, kept across retried walks: a walk that journals this turn's
        # input and then fails must not hide it from the next one.
        self._journaled_before: set[str] | None = None
        # Every user.message this IO saw on its live stream. The stream carries
        # only new events, so one seen there is this turn's.
        self._live_inputs: set[str] = set()
        if persistence is not None:
            persistence.check_session(scope, session)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        inputs = neutral_inputs(events)
        kind = _send_kind(inputs)
        # Checked only with a journal to compare against, and only for a send
        # this process posts: a restored receipt replays as before.
        checked = (
            self._persistence is not None
            and not self._own_inputs
            and any(isinstance(event, UserMessage) and event.mode == "new_turn" for event in inputs)
        )
        posted = False

        async def deliver(key: str) -> SendReceipt:
            nonlocal posted
            posted = True
            return await self._backend.events.send(self._scope, self._session, inputs, key=key)

        try:
            receipt = await _native_error_edge(
                deliver(str(uuid4()))
                if self._persistence is None
                else self._persistence.mutate(
                    self._session,
                    kind,
                    [event.model_dump(mode="json") for event in inputs],
                    deliver,
                    SendReceipt,
                    lambda receipt: "accepted" if receipt.status == "queued" else receipt.status,
                )
            )
        except BaseException as error:
            # Posted from this process with no answer: a reconnect must find it.
            self._unconfirmed = self._unconfirmed or (checked and posted)
            if isinstance(error, UncertainSend):
                raise TurnConnectionLost(str(error)) from error
            raise
        if checked and posted and receipt.input_ids:
            self._own_inputs = receipt.input_ids
        elif checked and posted and receipt.status == "outcome_unknown":
            self._unconfirmed = True
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
        async def deliver(key: str) -> Operation:
            return await self._backend.sessions.archive(self._scope, self._session, key=key)

        try:
            await _native_error_edge(
                deliver(str(uuid4()))
                if self._persistence is None
                else self._persistence.mutate(
                    self._session,
                    "archive",
                    {},
                    deliver,
                    Operation,
                    lambda receipt: receipt.status,
                )
            )
        except UncertainSend as error:
            raise TurnConnectionLost(str(error)) from error

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        # The injected Events implementation owns its read timeout. The default
        # Anthropic composition receives this value when the host builds it.
        source = await _native_error_edge(
            self._backend.events.open_stream(self._scope, self._session)
        )
        return _MuxStream(source, self._session, self._record, self._gap)

    async def _gap(self) -> None:
        if self._persistence is not None:
            await self._persistence.gap(self._session)

    async def _record(self, event: Event, *, live: bool = True) -> None:
        if self._persistence is not None:
            await self._persistence.record(self._session, event)
        if live and (input_id := _input_id(event)) is not None:
            self._live_inputs.add(input_id)
        if event.authority != "preview":
            self._remember_root(event)

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
            # What the journal held before the first walk; see _find_own_input.
            if self._unconfirmed and self._journaled_before is None:
                self._journaled_before = await self._journaled_ids()
            journaled = self._journaled_before if self._unconfirmed else None
            walked: list[tuple[str | None, bool]] = []
            source = history.walk(self._scope, self._session)
            try:
                while True:
                    try:
                        event = await _native_error_edge(source.__anext__())
                    except StopAsyncIteration:
                        break
                    await self._record(event, live=False)
                    if event.authority == "preview":
                        continue
                    walked.append((event.native.event_id, _input_id(event) is not None))
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
            self._find_own_input(walked, journaled)
            return events

        try:
            return await asyncio.wait_for(walk(), timeout=timeout_s)
        except TimeoutError as error:
            raise TurnError(
                kind="upstream", message=f"MA event replay did not complete within {timeout_s}s"
            ) from error

    async def _journaled_ids(self) -> set[str] | None:
        if self._persistence is None:
            return None
        found: set[str] = set()
        after = -1
        while page := await self._persistence.store.read_events(self._session.id, after=after):
            found.update(e.native.event_id for e in page if e.native.event_id is not None)
            after = page[-1].sequence
        return found

    def _find_own_input(
        self, walked: Sequence[tuple[str | None, bool]], journaled: set[str] | None
    ) -> None:
        """A replay must show this turn's own input before it reads an answer (F12).

        Without it, an earlier turn's ending would read as this turn's answer.
        Missing input is a lost connection: the person asks what was done.
        """
        inputs = [event_id for event_id, is_input in walked if is_input and event_id]
        if self._own_inputs:
            if not set(self._own_inputs) & set(inputs):
                raise TurnConnectionLost("this turn's input is not in the session history")
            return
        if not self._unconfirmed or journaled is None:
            return
        # Only an input after the newest event the journal already held can be
        # this turn's (or one its live stream saw): a hole earlier in the
        # journal is not new. With nothing journaled (a new session's first
        # turn, the designed reattach) only a session whose sole input is this
        # one can prove it. A session whose earlier turns were never journaled
        # reads the same; that residue needs a pre-send read the wire forbids.
        anchor = max(
            (i for i, (event_id, _) in enumerate(walked) if event_id in journaled), default=None
        )
        if anchor is None:
            mine = inputs if len(inputs) == 1 else []
        else:
            mine = [
                event_id
                for i, (event_id, is_input) in enumerate(walked)
                if is_input and event_id and i > anchor
            ]
        mine += [event_id for event_id in inputs if event_id in self._live_inputs]
        if not mine:
            raise TurnConnectionLost("this turn's input did not reach the session")
        self._own_inputs, self._unconfirmed = (mine[-1],), False

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        turn_id = self._turn_id or (
            self._persistence.operation_key if self._persistence is not None else self._operation_id
        )

        async def deliver(key: str) -> CancelReceipt:
            return await self._backend.events.cancel(
                self._scope, self._session, turn_id=turn_id, key=key
            )

        async def request_cancel() -> CancelReceipt:
            receipt = await _native_error_edge(
                deliver(str(uuid4()))
                if self._persistence is None
                else self._persistence.mutate(
                    self._session,
                    "cancel",
                    {"turn_id": turn_id},
                    deliver,
                    CancelReceipt,
                    lambda receipt: (
                        "outcome_unknown" if receipt.status == "outcome_unknown" else "accepted"
                    ),
                )
            )
            if receipt.session != self._session:
                raise ProviderError(
                    "upstream", retryable=False, native_code="foreign_cancel_receipt"
                )
            return receipt

        try:
            # Only the native Anthropic adapter needs main's pre-POST SSE
            # subscription. Class lookup avoids inventing a capability on mocks.
            subscribed = getattr(type(self._backend.events), "wait_stopped_before_cancel", None)
            if self._session.provider == "anthropic" and callable(subscribed):
                deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
                receipt, stopped = await _native_error_edge(
                    cast(_SubscribedCancel, self._backend.events).wait_stopped_before_cancel(
                        self._scope, self._session, cancel=request_cancel, deadline=deadline
                    )
                )
            else:
                receipt = await request_cancel()
                deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
                stopped = await _native_error_edge(
                    self._backend.events.wait_stopped(
                        self._scope,
                        receipt,
                        deadline=deadline,
                    )
                )
        except UncertainSend as error:
            raise TurnConnectionLost(str(error)) from error
        except TimeoutError as error:
            raise TurnError(
                kind="interrupt_timeout",
                message=f"MA did not acknowledge interrupt within {timeout_s}s",
            ) from error
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
        if self._persistence is not None:
            await self._persistence.stopped(receipt.operation_id, self._session)
        return stopped


def default_mux_turn_io(
    client: anthropic.AsyncAnthropic,
    scope: Scope,
    session_id: str,
    *,
    read_timeout_s: float,
    persistence: TurnPersistence | None = None,
) -> MuxTurnIO:
    """Compose ports around the existing client and its authorized session.

    Sessions are registered in the composable factory. No lifecycle
    implementation, credential lookup or provider I/O belongs here.
    """
    bound = turn_backend(
        TurnBackendRequest(
            "anthropic.managed_agents",
            client,
            scope,
            session_id,
            read_timeout_s,
            on_stop_event=persistence.record if persistence is not None else None,
        )
    )
    return MuxTurnIO(bound.backend, scope, bound.session, persistence=persistence)


def _anthropic_turn_codec(request: TurnCodecRequest) -> TurnIO:
    return MuxTurnIO(
        request.backend, request.scope, request.session, persistence=request.persistence
    )


register_turn_codec("anthropic.managed_agents", _anthropic_turn_codec)


def turn_io(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    *,
    path: Literal["legacy", "mux"] | None = None,
    backend: ManagedAgents | None = None,
    scope: Scope | None = None,
    session_ref: ResourceRef | None = None,
    read_timeout_s: float = 120.0,
    profile: str | None = None,
    backend_request: TurnBackendRequest | None = None,
    persistence: TurnPersistence | None = None,
    provider_actions: ProviderActionApproval | None = None,
) -> TurnIO:
    """Bind every turn helper to the same authorized session as its driver."""
    if profile is None and backend_request is not None:
        profile = backend_request.profile
    selected = path if path is not None else load_turn_settings().path
    if selected == "legacy":
        if profile not in (None, "anthropic.managed_agents"):
            raise UnsupportedCapability(("mux_turn_path",), profile)
        return LegacyTurnIO(client, session_id, scope=scope)
    if scope is None:
        raise ScopeViolation(session_id, "mux turns require the caller's authorized scope")
    selected_profile = profile or (
        backend.capabilities().profile_id if backend is not None else "anthropic.managed_agents"
    )
    codec = _TURN_CODECS.get(selected_profile)
    if codec is None:
        from daimon.core.turn.runtimes import ensure_channel_runtime

        if ensure_channel_runtime(selected_profile, ready=lambda: selected_profile in _TURN_CODECS):
            codec = _TURN_CODECS.get(selected_profile)
    if codec is None:
        raise UnsupportedCapability(("host_turn_codec",), selected_profile)
    if backend_request is not None and (
        backend_request.profile != selected_profile
        or backend_request.scope != scope
        or backend_request.session_id != session_id
        or backend_request.client is not client
        or backend_request.read_timeout_s != read_timeout_s
        or (
            backend_request.config is not None
            and (
                backend_request.config.profile != selected_profile
                or backend_request.config.channel.tenant_id != scope.tenant_id
            )
        )
    ):
        raise ScopeViolation(session_id, "backend request differs from this turn")
    if backend is None:
        request = backend_request or TurnBackendRequest(
            selected_profile, client, scope, session_id, read_timeout_s, session=session_ref
        )
        if persistence is not None:
            request = replace(request, on_stop_event=persistence.record)
        bound = turn_backend(request)
        backend, session_ref = bound.backend, bound.session
    if session_ref is None or session_ref.id != session_id:
        raise ScopeViolation(session_id, "an injected backend requires the bound session ref")
    if backend_request is not None and (
        backend_request.session is not None and backend_request.session != session_ref
    ):
        raise ScopeViolation(session_id, "codec differs from the authorized native binding")
    capabilities = backend.capabilities()
    if capabilities.profile_id != selected_profile or (
        session_ref.kind != "session"
        or session_ref.provider != capabilities.provider
        or (
            not (scope.is_platform or scope.is_legacy_host_authorized)
            and (
                session_ref.tenant_id != scope.tenant_id
                or session_ref.account_id != scope.account_id
            )
        )
    ):
        raise ScopeViolation(session_id, "turn codec differs from the admitted profile or session")
    return codec(
        TurnCodecRequest(
            backend,
            scope,
            session_ref,
            read_timeout_s,
            backend_request.config if backend_request is not None else None,
            backend_request.runtime if backend_request is not None else None,
            persistence,
            provider_actions.bind(scope, session_ref) if provider_actions is not None else None,
        )
    )
