"""Scoped OpenAI turn transport, registered by explicit native host preparation.

Only neutral ports cross this boundary. Display DTOs retain the real neutral
event, and token observations are fetched separately without SDK meter spans.
The injected persistence edge owns leases, claims and durable receipt replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
)
from daimon.core.errors import TurnError
from daimon.core.ma import REPLAY_TIMEOUT_S
from daimon.core.turn.io import TurnConnectionLost, TurnEvent, TurnStream
from daimon.core.turn.openai_codec import PROFILE, display_batch
from daimon.core.turn.persistence import UncertainSend
from mux.contracts.actions import InputEvent, UserMessage, UserToolConfirmation
from mux.contracts.events import (
    Event,
    ImagePart,
    NativeProvenance,
    StatusRunningPayload,
    TextPart,
    TurnEndedPayload,
)
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, OperationStatus, SendReceipt, StopObservation
from mux.contracts.usage import UsageObservation
from mux.drivers.openai.normalize import SCHEMA_DATE
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from pydantic import BaseModel, JsonValue, TypeAdapter

_INPUTS = TypeAdapter(list[dict[str, JsonValue]])
_PAGE_BOUND = 50
_BUFFER_BOUND = 1000


class OpenAIPersistence(Protocol):
    """Structural bridge to N4's journal and claimed-mutation context."""

    async def record(self, session: ResourceRef, event: Event) -> None: ...
    async def stopped(self, key: str, session: ResourceRef) -> None: ...
    async def gap(self, session: ResourceRef) -> None: ...
    async def mutate[T: BaseModel](
        self,
        session: ResourceRef,
        kind: str,
        request: JsonValue,
        call: Callable[[str], Awaitable[T]],
        response: type[T],
        status: Callable[[T], OperationStatus],
        /,
    ) -> T: ...


def _timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("turn timeout must be positive and finite")
    return value


async def _edge[T](work: Awaitable[T]) -> T:
    try:
        return await work
    except UncertainSend:
        raise TurnConnectionLost("OpenAI delivery was claimed; replay before continuing") from None
    except ProviderError as error:
        if error.category == "transient_network":
            raise TurnConnectionLost("OpenAI connection lost; replay before continuing") from None
        raise


def _inputs(events: Sequence[BetaManagedAgentsEventParams]) -> tuple[InputEvent, ...]:
    result: list[InputEvent] = []
    for event in _INPUTS.validate_python(events):
        if event.get("type") == "user.message":
            raw = event.get("content")
            if not isinstance(raw, list) or not raw:
                raise UnsupportedCapability(("host_input_content",), PROFILE)
            parts: list[TextPart | ImagePart] = []
            for part in raw:
                if not isinstance(part, dict):
                    raise UnsupportedCapability(("host_input_content",), PROFILE)
                if part.get("type") == "text":
                    parts.append(TextPart.model_validate(part))
                elif part.get("type") == "image":
                    source = part.get("source")
                    if not isinstance(source, dict) or source.get("type") != "base64":
                        raise UnsupportedCapability(("host_input_image",), PROFILE)
                    parts.append(
                        ImagePart.model_validate(
                            {
                                "media_type": source.get("media_type"),
                                "data_base64": source.get("data"),
                            }
                        )
                    )
                else:
                    raise UnsupportedCapability(("host_input_content",), PROFILE)
            result.append(UserMessage(content=tuple(parts)))
        elif event.get("type") == "user.tool_confirmation":
            identity = event.get("tool_use_id")
            if not isinstance(identity, str) or not identity.startswith("openai:tool:"):
                raise UnsupportedCapability(("host_origin_confirmation_identity",), PROFILE)
            result.append(
                UserToolConfirmation.model_validate(
                    {
                        "action_id": identity.removeprefix("openai:tool:"),
                        "decision": event.get("result"),
                        "deny_message": event.get("deny_message"),
                    }
                )
            )
        else:
            # The Agents input schema accepts user messages, not system/developer
            # messages. Privileged framing must be bound by preparation instead.
            raise UnsupportedCapability(("host_turn_input",), PROFILE)
    if not result or sum(isinstance(item, UserMessage) for item in result) > 1:
        raise UnsupportedCapability(("host_turn_input_batch",), PROFILE)
    return tuple(result)


def _send_status(receipt: SendReceipt) -> OperationStatus:
    return "accepted" if receipt.status == "queued" else receipt.status


def _cancel_status(receipt: CancelReceipt) -> OperationStatus:
    if receipt.status == "already_stopped":
        return "processed"
    return "outcome_unknown" if receipt.status == "outcome_unknown" else "accepted"


class _OpenAIStream(AsyncIterator[TurnEvent]):
    def __init__(self, source: AsyncIterator[Event], owner: OpenAITurnIO) -> None:
        self._source, self._owner = source, owner
        self._pending: deque[TurnEvent] = deque()
        self._closed = False

    def __aiter__(self) -> _OpenAIStream:
        return self

    async def __anext__(self) -> TurnEvent:
        while not self._closed:
            if self._pending:
                return self._pending.popleft()
            try:
                event = await _edge(self._source.__anext__())
            except StopAsyncIteration:
                ended = await self._owner.end_stream()
                await self.close()
                if not ended:
                    raise TurnConnectionLost("OpenAI stream ended without a root outcome") from None
                raise
            except BaseException:
                await self.close()
                raise
            try:
                self._pending.extend(await self._owner.accept_record(event))
            except BaseException:
                await self.close()
                raise
        raise StopAsyncIteration

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pending.clear()
            close = getattr(self._source, "aclose", None)
            if callable(close):
                await cast(Callable[[], Awaitable[None]], close)()


class OpenAITurnIO:
    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: OpenAIPersistence,
        root_turn_id: str | None = None,
        baseline_loader: Callable[
            [ResourceRef, tuple[str, ...]], Awaitable[tuple[tuple[str, ...], bool]]
        ]
        | None = None,
    ) -> None:
        if (
            backend.capabilities().profile_id != PROFILE
            or session.provider != "openai"
            or session.kind != "session"
            or session.tenant_id != scope.tenant_id
            or session.account_id != scope.account_id
        ):
            raise ScopeViolation(session.id, "foreign OpenAI host binding")
        if root_turn_id == "":
            raise ValueError("empty root turn identity")
        self._backend, self._scope, self._session = backend, scope, session
        self._persistence = persistence
        self._baseline_loader = baseline_loader
        self._resuming = False
        self._root = root_turn_id
        self._prior_roots: set[str] | None = None
        self._input_started = root_turn_id is not None
        self._buffered: list[Event] = []
        self._displayed: dict[str, str] = {}
        self._terminal: str | None = None
        self._terminated = False

    def _validate(self, event: Event) -> None:
        if event.session_id != self._session.id or event.native.provider != "openai":
            raise ScopeViolation(self._session.id, "foreign OpenAI host event")

    async def _history(self) -> list[Event]:
        values: list[Event] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_PAGE_BOUND):
            page = await _edge(
                self._backend.events.list(
                    self._scope,
                    self._session,
                    page=PageRequest(cursor=cursor, order="asc", limit=100),
                )
            )
            for event in page.data:
                self._validate(event)
            values.extend(page.data)
            if not page.has_more:
                return values
            cursor = page.next_cursor
            if not cursor or cursor in seen:
                raise ProviderError("upstream", retryable=False, native_code="host_history_cursor")
            seen.add(cursor)
        raise ProviderError("upstream", retryable=False, native_code="host_history_bound")

    async def _baseline(self) -> None:
        if self._prior_roots is None:
            values = await self._history()
            self._prior_roots = {e.turn_id for e in values if e.turn_id is not None}
            if self._baseline_loader is not None:
                roots, resumed = await self._baseline_loader(
                    self._session, tuple(sorted(self._prior_roots))
                )
                self._prior_roots = set(roots)
                self._resuming = resumed
                self._input_started = self._input_started or resumed
            if self._root is not None:
                self._prior_roots.discard(self._root)

    def _remember(self, event: Event) -> None:
        if (
            self._root is not None
            or event.thread_id is not None
            or not self._input_started
            or event.authority not in ("record", "reconciled")
        ):
            return
        payload = event.typed_payload()
        if isinstance(payload, StatusRunningPayload | TurnEndedPayload):
            if event.turn_id != payload.root_turn_id:
                raise ValueError("root payload disagrees with event identity")
            if event.turn_id not in (self._prior_roots or ()):
                self._root = event.turn_id

    def _frames(self, event: Event) -> list[TurnEvent]:
        if event.thread_id is not None or (
            event.turn_id != self._root and event.type != "session.status_terminated"
        ):
            return []
        if event.type == "session.turn_ended":
            payload = TurnEndedPayload.model_validate(event.payload)
            self._check_terminal(event)
            self._terminal = payload.outcome
            if payload.outcome == "errored":
                # accept_record durably journals this root before display.
                # An SDK retries_exhausted idle is not a foreign failure.
                raise ProviderError("upstream", retryable=False, native_code="host_root_failed")
        frames: list[TurnEvent] = []
        if event.type == "session.status_terminated":
            self._terminated = True
        for display in display_batch(event, session_id=self._session.id):
            encoded = display.model_dump_json(exclude={"processed_at"})
            if self._displayed.get(display.id) != encoded:
                self._displayed[display.id] = encoded
                frames.append(TurnEvent(display, event))
        return frames

    async def end_stream(self) -> bool:
        if self._terminal is not None or self._terminated:
            return True
        await self._persistence.gap(self._session)
        return False

    def _check_terminal(self, event: Event) -> None:
        if (
            event.type == "session.turn_ended"
            and event.turn_id == self._root
            and event.authority in ("record", "reconciled")
            and event.thread_id is None
        ):
            payload = TurnEndedPayload.model_validate(event.payload)
            if self._terminal is not None and self._terminal != payload.outcome:
                raise ProviderError(
                    "upstream", retryable=False, native_code="conflicting_root_outcome"
                )

    async def accept_record(self, event: Event) -> list[TurnEvent]:
        self._validate(event)
        if event.authority == "preview":
            return []
        self._check_terminal(event)
        await self._persistence.record(self._session, event)
        self._remember(event)
        if self._root is None and event.type != "session.status_terminated":
            if len(self._buffered) >= _BUFFER_BOUND:
                raise ProviderError(
                    "upstream", retryable=False, native_code="host_root_buffer_bound"
                )
            self._buffered.append(event)
            return []
        pending, self._buffered = self._buffered, []
        return [frame for value in (*pending, event) for frame in self._frames(value)]

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        inputs = _inputs(events)
        new_turn = any(isinstance(item, UserMessage) for item in inputs)
        if new_turn and self._input_started and not self._resuming:
            raise UnsupportedCapability(("host_one_root_per_turn",), PROFILE)
        async with asyncio.timeout(REPLAY_TIMEOUT_S):
            # The configured driver verifies the admitted model and explicitly
            # disabled delegation on a fresh native session before every input.
            current = await _edge(self._backend.sessions.retrieve(self._scope, self._session))
            if current.ref != self._session:
                raise ScopeViolation(self._session.id, "foreign OpenAI input session")
            await self._baseline()
        if new_turn:
            self._input_started = True
            self._resuming = False
        actions = [item.action_id for item in inputs if isinstance(item, UserToolConfirmation)]
        kind = (
            "send"
            if not actions
            else "origin:"
            + hashlib.sha256(
                json.dumps(sorted(actions), separators=(",", ":")).encode()
            ).hexdigest()[:32]
        )

        async def deliver(key: str) -> SendReceipt:
            return await self._backend.events.send(self._scope, self._session, inputs, key=key)

        receipt = await _edge(
            self._persistence.mutate(
                self._session,
                kind,
                {"events": [item.model_dump(mode="json") for item in inputs]},
                deliver,
                SendReceipt,
                _send_status,
            )
        )
        if receipt.status == "outcome_unknown":
            raise TurnConnectionLost("OpenAI input outcome unknown; replay without resending")
        if receipt.status not in ("queued", "processed"):
            raise ProviderError("upstream", retryable=False, native_code="host_send_rejected")

    async def status(self) -> str:
        session = await _edge(self._backend.sessions.retrieve(self._scope, self._session))
        if session.ref != self._session:
            raise ScopeViolation(self._session.id, "foreign OpenAI status")
        return {"provisioning": "rescheduling", "requires_action": "idle"}.get(
            session.state, session.state
        )

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        async with asyncio.timeout(_timeout(read_timeout_s)):
            await self._baseline()
            source = await _edge(self._backend.events.open_stream(self._scope, self._session))
        return _OpenAIStream(source, self)

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        async with asyncio.timeout(_timeout(timeout_s)):
            await self._baseline()
            snapshot = await _edge(self._backend.events.reconcile(self._scope, self._session))
            if snapshot.session != self._session:
                raise ScopeViolation(self._session.id, "foreign OpenAI recovery")
            values = await self._history()
            candidates = {
                value.turn_id
                for value in values
                if value.thread_id is None
                and value.turn_id is not None
                and value.turn_id not in (self._prior_roots or ())
                and value.type in ("session.status_running", "session.turn_ended")
            }
            if self._root is None and self._input_started:
                if len(candidates) > 1:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="ambiguous_host_root"
                    )
                if candidates:
                    self._root = next(iter(candidates))
            # Commit native order; reorder only display records. Saved turn
            # pages precede saved item pages, including their own terminal.
            outcomes = {
                TurnEndedPayload.model_validate(value.payload).outcome
                for value in values
                if value.type == "session.turn_ended"
                and value.turn_id == self._root
                and value.thread_id is None
                and value.authority in ("record", "reconciled")
            }
            if len(outcomes) > 1:
                raise ProviderError(
                    "upstream", retryable=False, native_code="conflicting_root_outcome"
                )
            for value in values:
                self._check_terminal(value)
            for value in values:
                await self._persistence.record(self._session, value)
            current = [
                value
                for value in values
                if value.turn_id == self._root
                and self._root is not None
                or value.type == "session.status_terminated"
            ]
            current.sort(
                key=lambda value: value.type in ("session.turn_ended", "session.status_terminated")
            )
            # The completed recovery snapshot has already been journaled.
            # Replay DTOs alone cannot carry a foreign terminal outcome into
            # the legacy finalizer. Use its existing explicit failure/interrupt
            # paths; neither retries delivery or sends another cancel.
            if "errored" in outcomes:
                raise ProviderError("upstream", retryable=False, native_code="host_root_failed")
            if "interrupted" in outcomes:
                from daimon.core.turn.driver import InterruptedDuringRecovery

                raise InterruptedDuringRecovery(phase="replay")
            result: list[BetaManagedAgentsSessionEvent] = []
            for value in current:
                if value.authority not in ("record", "reconciled") or value.thread_id is not None:
                    continue
                if value.type == "session.turn_ended":
                    payload = TurnEndedPayload.model_validate(value.payload)
                    if self._terminal is not None and self._terminal != payload.outcome:
                        raise ProviderError(
                            "upstream", retryable=False, native_code="conflicting_root_outcome"
                        )
                    self._terminal = payload.outcome
                result.extend(display_batch(value, session_id=self._session.id))
            return result

    async def replay_usage(self, *, timeout_s: float = 8.0) -> tuple[UsageObservation, ...]:
        if self._root is None:
            return ()
        async with asyncio.timeout(_timeout(timeout_s)):
            values: list[UsageObservation] = []
            cursor: str | None = None
            seen: set[str] = set()
            for _ in range(5):
                page = await _edge(
                    self._backend.usage.list(
                        self._scope,
                        self._session,
                        page=PageRequest(cursor=cursor, order="desc", limit=100),
                    )
                )
                for observation in page.data:
                    if observation.session != self._session:
                        raise ScopeViolation(self._session.id, "foreign OpenAI usage")
                    if observation.turn_id == self._root and observation.thread_id is None:
                        values.append(observation)
                if not page.has_more:
                    return tuple(values)
                cursor = page.next_cursor
                if not cursor or cursor in seen:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="host_usage_cursor"
                    )
                seen.add(cursor)
            raise ProviderError("upstream", retryable=False, native_code="host_usage_bound")

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        deadline = datetime.now(UTC) + timedelta(seconds=_timeout(timeout_s))
        try:
            async with asyncio.timeout(timeout_s):
                if self._root is None:
                    session = await _edge(
                        self._backend.sessions.retrieve(self._scope, self._session)
                    )
                    if session.ref != self._session:
                        raise ScopeViolation(self._session.id, "foreign OpenAI interrupt target")
                    root = session.active_root_turn
                    if root is None or root in (self._prior_roots or ()) or not self._input_started:
                        raise UnsupportedCapability(("host_cancel_root_identity",), PROFILE)
                    self._root = root

                async def deliver(key: str) -> CancelReceipt:
                    return await self._backend.events.cancel(
                        self._scope, self._session, turn_id=self._root or "", key=key
                    )

                receipt = await _edge(
                    self._persistence.mutate(
                        self._session,
                        "cancel",
                        {"turn_id": self._root},
                        deliver,
                        CancelReceipt,
                        _cancel_status,
                    )
                )
                if receipt.session != self._session or receipt.turn_id != self._root:
                    raise ScopeViolation(self._session.id, "foreign OpenAI cancellation receipt")
                while True:
                    observed = await _edge(
                        self._backend.events.wait_stopped(self._scope, receipt, deadline=deadline)
                    )
                    if observed.receipt_operation_id != receipt.operation_id:
                        raise ProviderError(
                            "upstream", retryable=False, native_code="foreign_stop_observation"
                        )
                    if observed.stopped:
                        if observed.outcome is None:
                            raise ProviderError(
                                "upstream", retryable=False, native_code="missing_stop_outcome"
                            )
                        stopped = Event(
                            id="openai:stop:" + receipt.operation_id,
                            session_id=self._session.id,
                            sequence=0,
                            type="session.turn_ended",
                            turn_id=receipt.turn_id,
                            observed_at=observed.observed_at,
                            authority="record",
                            payload=TurnEndedPayload(
                                root_turn_id=receipt.turn_id,
                                outcome=observed.outcome,
                                native_reason="observed_stop",
                            ).model_dump(mode="json"),
                            native=NativeProvenance(
                                provider="openai",
                                api_revision=SCHEMA_DATE,
                                event_type="turn_stop_observation",
                            ),
                        )
                        self._check_terminal(stopped)
                        await self._persistence.record(self._session, stopped)
                        await self._persistence.stopped(receipt.operation_id, self._session)
                        self._terminal = observed.outcome
                        return observed
                    await asyncio.sleep(
                        min(0.1, max(0, (deadline - datetime.now(UTC)).total_seconds()))
                    )
        except TimeoutError:
            raise TurnError(
                kind="interrupt_timeout",
                message="OpenAI root stop was not observed within the deadline",
            ) from None

    async def archive(self) -> None:
        # The provider has no archive primitive. Preserve that typed refusal;
        # retiring a host binding must never silently hard-delete its session.
        raise UnsupportedCapability(("session_archive",), PROFILE)
