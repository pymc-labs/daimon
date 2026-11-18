"""Scoped OpenAI and self-hosted turn transport, registered by native host preparation.

Only neutral ports cross this boundary. Display DTOs retain the real neutral
event, and token observations are fetched separately without SDK meter spans.
The injected persistence edge owns leases, claims and durable receipt replay.
`HostedTurnIO` holds the shared flow; each subclass supplies how it pages
history, takes its baseline, resolves approvals and observes a stop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Protocol, cast

from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolUseEvent,
    BetaManagedAgentsAgentToolUseEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
)
from daimon.core.errors import TurnError
from daimon.core.ma import REPLAY_TIMEOUT_S
from daimon.core.turn import openai_state, selfhosted_state
from daimon.core.turn.host_spend_cap import SpendGuard
from daimon.core.turn.hosted_codec import (
    OPENAI,
    SELFHOSTED,
    DisplayEvent,
    HostedCodec,
    display_batch,
)
from daimon.core.turn.io import TurnConnectionLost, TurnEvent, TurnStream
from daimon.core.turn.persistence import UncertainSend
from mux.contracts.actions import InputEvent, UserMessage, UserToolConfirmation
from mux.contracts.events import (
    Event,
    ImagePart,
    NativeProvenance,
    RequiresActionPayload,
    StatusRunningPayload,
    TextPart,
    TurnEndedPayload,
)
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, OperationStatus, SendReceipt, StopObservation
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import SCHEMA_DATE, OpenAIUsage
from mux.drivers.selfhosted import SelfHostedUsage
from mux.drivers.selfhosted.wire import CLEANUP_EXHAUSTED
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from pydantic import BaseModel, JsonValue, TypeAdapter

_INPUTS = TypeAdapter(list[dict[str, JsonValue]])
_BUFFER_BOUND = 1000


class HostedPersistence(Protocol):
    """Structural bridge to the journal and claimed-mutation context."""

    async def record(
        self, session: ResourceRef, event: Event, *, source_key: str | None = None
    ) -> None: ...
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


def root_failed(native_reason: str | None) -> ProviderError:
    """The error for an errored root; a sealed self-hosted conversation keeps its reason."""
    code = CLEANUP_EXHAUSTED if native_reason == CLEANUP_EXHAUSTED else "host_root_failed"
    return ProviderError("upstream", retryable=False, native_code=code)


def _timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("turn timeout must be positive and finite")
    return value


async def _edge[T](work: Awaitable[T], codec: HostedCodec) -> T:
    try:
        return await work
    except UncertainSend:
        raise TurnConnectionLost(
            f"{codec.label} delivery was claimed; replay before continuing"
        ) from None
    except ProviderError as error:
        if error.category == "transient_network":
            raise TurnConnectionLost(
                f"{codec.label} connection lost; replay before continuing"
            ) from None
        raise


def _inputs(
    events: Sequence[BetaManagedAgentsEventParams], codec: HostedCodec
) -> tuple[InputEvent, ...]:
    profile, prefix = codec.profile, codec.tool_display_id("")
    result: list[InputEvent] = []
    for event in _INPUTS.validate_python(events):
        if event.get("type") == "user.message":
            raw = event.get("content")
            if not isinstance(raw, list) or not raw:
                raise UnsupportedCapability(("host_input_content",), profile)
            parts: list[TextPart | ImagePart] = []
            for part in raw:
                if not isinstance(part, dict):
                    raise UnsupportedCapability(("host_input_content",), profile)
                if part.get("type") == "text":
                    parts.append(TextPart.model_validate(part))
                elif part.get("type") == "image":
                    source = part.get("source")
                    if not isinstance(source, dict) or source.get("type") != "base64":
                        raise UnsupportedCapability(("host_input_image",), profile)
                    parts.append(
                        ImagePart.model_validate(
                            {
                                "media_type": source.get("media_type"),
                                "data_base64": source.get("data"),
                            }
                        )
                    )
                else:
                    raise UnsupportedCapability(("host_input_content",), profile)
            result.append(UserMessage(content=tuple(parts)))
        elif event.get("type") == "user.tool_confirmation":
            identity = event.get("tool_use_id")
            if not isinstance(identity, str) or not identity.startswith(prefix):
                raise UnsupportedCapability(("host_origin_confirmation_identity",), profile)
            result.append(
                UserToolConfirmation.model_validate(
                    {
                        "action_id": identity.removeprefix(prefix),
                        "decision": event.get("result"),
                        "deny_message": event.get("deny_message"),
                    }
                )
            )
        else:
            # The input schema accepts user messages, not system/developer
            # messages. Privileged framing must be bound by preparation instead.
            raise UnsupportedCapability(("host_turn_input",), profile)
    if not result or sum(isinstance(item, UserMessage) for item in result) > 1:
        raise UnsupportedCapability(("host_turn_input_batch",), profile)
    return tuple(result)


def _send_status(receipt: SendReceipt) -> OperationStatus:
    return "accepted" if receipt.status == "queued" else receipt.status


def _cancel_status(receipt: CancelReceipt) -> OperationStatus:
    if receipt.status == "already_stopped":
        return "processed"
    return "outcome_unknown" if receipt.status == "outcome_unknown" else "accepted"


class _HostedStream(AsyncIterator[TurnEvent]):
    def __init__(
        self,
        source: AsyncIterator[Event],
        owner: HostedTurnIO,
        initial: Sequence[TurnEvent] = (),
    ) -> None:
        self._source, self._owner = source, owner
        self._pending: deque[TurnEvent] = deque(initial)
        self._closed = False

    def __aiter__(self) -> _HostedStream:
        return self

    async def __anext__(self) -> TurnEvent:
        while not self._closed:
            if self._pending:
                return self._pending.popleft()
            try:
                event = await _edge(self._source.__anext__(), self._owner.codec)
            except StopAsyncIteration:
                ended = await self._owner.end_stream()
                await self.close()
                if not ended:
                    raise TurnConnectionLost(
                        f"{self._owner.codec.label} stream ended without a root outcome"
                    ) from None
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


class HostedTurnIO:
    """The turn transport both hosted backends share; subclasses fill the hooks."""

    codec: ClassVar[HostedCodec]
    #: The driver asks for the session status whenever the stream yields nothing for
    #: this long; None never asks.
    idle_check_s: float | None = None
    #: Usage pages read, newest first, before the root turn counts as missing.
    _usage_pages: ClassVar[int]
    _usage_page_size: ClassVar[int]

    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: HostedPersistence,
        root_turn_id: str | None = None,
    ) -> None:
        codec = self.codec
        if (
            backend.capabilities().profile_id not in codec.profiles
            or session.provider != codec.provider
            or session.kind != "session"
            or session.tenant_id != scope.tenant_id
            or session.account_id != scope.account_id
        ):
            raise ScopeViolation(session.id, f"foreign {codec.label} host binding")
        if root_turn_id == "":
            raise ValueError("empty root turn identity")
        self._backend, self._scope, self._session = backend, scope, session
        self._persistence = persistence
        self._resuming = False
        self._root = root_turn_id
        self._prior_roots: set[str] | None = None
        self._input_started = root_turn_id is not None
        self._buffered: list[Event] = []
        self._displayed: dict[str, str] = {}
        self._terminal: str | None = None
        self._terminated = False

    # -- hooks -----------------------------------------------------------------

    async def _history(self) -> list[Event]:
        raise NotImplementedError

    async def _take_baseline(self) -> None:
        """Set `_prior_roots` (and any resume state) on first use."""
        raise NotImplementedError

    def _display(self, event: Event) -> tuple[DisplayEvent, ...]:
        return display_batch(event, session_id=self._session.id, codec=self.codec)

    def _journal_source(self, event: Event) -> str:
        raise NotImplementedError

    def _root_error(self, native_reason: str | None) -> ProviderError:
        return ProviderError("upstream", retryable=False, native_code="host_root_failed")

    async def _after_record(self, event: Event) -> None:
        """Runs after an event's frames are built, before they are shown."""

    async def _stop_event(self, receipt: CancelReceipt, observed: StopObservation) -> Event:
        raise NotImplementedError

    # -- shared flow -----------------------------------------------------------

    def _edge[T](self, work: Awaitable[T]) -> Awaitable[T]:
        return _edge(work, self.codec)

    def _validate(self, event: Event) -> None:
        if event.session_id != self._session.id or event.native.provider != self.codec.provider:
            raise ScopeViolation(self._session.id, f"foreign {self.codec.label} host event")

    async def _paged_history(self, cursor: str | None, *, bound: int | None) -> list[Event]:
        values: list[Event] = []
        seen: set[str] = set() if cursor is None else {cursor}
        pages = 0
        while bound is None or pages < bound:
            pages += 1
            page = await self._edge(
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
            await self._take_baseline()
            if self._root is not None and self._prior_roots is not None:
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
                raise self._root_error(payload.native_reason)
        frames: list[TurnEvent] = []
        if event.type == "session.status_terminated":
            self._terminated = True
        for display in self._display(event):
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
        await self._persistence.record(self._session, event, source_key=self._journal_source(event))
        self._remember(event)
        if self._root is None and event.type != "session.status_terminated":
            if len(self._buffered) >= _BUFFER_BOUND:
                raise ProviderError(
                    "upstream", retryable=False, native_code="host_root_buffer_bound"
                )
            self._buffered.append(event)
            return []
        pending, self._buffered = self._buffered, []
        frames = [frame for value in (*pending, event) for frame in self._frames(value)]
        await self._after_record(event)
        return frames

    async def _resolve_inputs(
        self, events: Sequence[BetaManagedAgentsEventParams], inputs: tuple[InputEvent, ...]
    ) -> tuple[InputEvent, ...] | None:
        """The inputs to deliver; None once they were delivered one by one."""
        return inputs

    def _expected_turn(self, new_turn: bool) -> str | None:
        return None

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        resolved = await self._resolve_inputs(events, _inputs(events, self.codec))
        if resolved is None:
            return
        inputs = resolved
        new_turn = any(isinstance(item, UserMessage) for item in inputs)
        if new_turn and self._input_started and not self._resuming:
            raise UnsupportedCapability(("host_one_root_per_turn",), self.codec.profile)
        async with asyncio.timeout(REPLAY_TIMEOUT_S):
            # The configured driver rechecks the admitted model and pinned
            # revisions on the native session before every input.
            current = await self._edge(self._backend.sessions.retrieve(self._scope, self._session))
            if current.ref != self._session:
                raise ScopeViolation(self._session.id, f"foreign {self.codec.label} input session")
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
        expected = self._expected_turn(new_turn)

        async def deliver(key: str) -> SendReceipt:
            if expected is None:
                return await self._backend.events.send(self._scope, self._session, inputs, key=key)
            return await self._backend.events.send(
                self._scope, self._session, inputs, key=key, expected_turn=expected
            )

        receipt = await self._edge(
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
            raise TurnConnectionLost(
                f"{self.codec.label} input outcome unknown; replay without resending"
            )
        if receipt.status not in ("queued", "processed"):
            raise ProviderError("upstream", retryable=False, native_code="host_send_rejected")

    async def status(self) -> str:
        session = await self._edge(self._backend.sessions.retrieve(self._scope, self._session))
        if session.ref != self._session:
            raise ScopeViolation(self._session.id, f"foreign {self.codec.label} status")
        return {"provisioning": "rescheduling", "requires_action": "idle"}.get(
            session.state, session.state
        )

    async def _open_source(self) -> AsyncIterator[Event]:
        return await self._edge(self._backend.events.open_stream(self._scope, self._session))

    async def _initial_frames(self) -> list[TurnEvent]:
        return []

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        async with asyncio.timeout(_timeout(read_timeout_s)):
            await self._baseline()
            source = await self._open_source()
        return _HostedStream(source, self, await self._initial_frames())

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        async with asyncio.timeout(_timeout(timeout_s)):
            await self._baseline()
            snapshot = await self._edge(self._backend.events.reconcile(self._scope, self._session))
            if snapshot.session != self._session:
                raise ScopeViolation(self._session.id, f"foreign {self.codec.label} recovery")
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
                await self._persistence.record(
                    self._session, value, source_key=self._journal_source(value)
                )
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
                raise self._root_error(
                    next(
                        (
                            TurnEndedPayload.model_validate(value.payload).native_reason
                            for value in current
                            if value.type == "session.turn_ended"
                            and value.authority in ("record", "reconciled")
                            and value.thread_id is None
                        ),
                        None,
                    )
                )
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
                result.extend(self._display(value))
            return result

    async def replay_usage(self, *, timeout_s: float = 8.0) -> tuple[UsageObservation, ...]:
        if self._root is None:
            return ()
        async with asyncio.timeout(_timeout(timeout_s)):
            values: list[UsageObservation] = []
            if isinstance(self._backend.usage, OpenAIUsage | SelfHostedUsage):
                # A listing reads every ended turn's items for its searches, and any
                # failed read can end a reconnect; read only the root turn.
                values.extend(
                    await self._edge(
                        self._backend.usage.reconcile_turn(self._scope, self._session, self._root)
                    )
                )
                if any(value.session != self._session for value in values):
                    raise ScopeViolation(self._session.id, f"foreign {self.codec.label} usage")
                return tuple(values)
            cursor: str | None = None
            seen: set[str] = set()
            # Newest first: a poll costs a few pages however long the thread is.
            for _ in range(self._usage_pages):
                page = await self._edge(
                    self._backend.usage.list(
                        self._scope,
                        self._session,
                        page=PageRequest(cursor=cursor, order="desc", limit=self._usage_page_size),
                    )
                )
                for observation in page.data:
                    if observation.session != self._session:
                        raise ScopeViolation(self._session.id, f"foreign {self.codec.label} usage")
                    if observation.turn_id == self._root and observation.thread_id is None:
                        values.append(observation)
                if self._usage_found(values) or not page.has_more:
                    return tuple(values)
                cursor = page.next_cursor
                if not cursor or cursor in seen:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="host_usage_cursor"
                    )
                seen.add(cursor)
            raise ProviderError("upstream", retryable=False, native_code="host_usage_bound")

    def _usage_found(self, values: list[UsageObservation]) -> bool:
        """Whether the newest-first usage walk may stop early."""
        return False

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        deadline = datetime.now(UTC) + timedelta(seconds=_timeout(timeout_s))
        label = self.codec.label
        try:
            async with asyncio.timeout(timeout_s):
                if self._root is None:
                    session = await self._edge(
                        self._backend.sessions.retrieve(self._scope, self._session)
                    )
                    if session.ref != self._session:
                        raise ScopeViolation(self._session.id, f"foreign {label} interrupt target")
                    root = session.active_root_turn
                    if root is None or root in (self._prior_roots or ()) or not self._input_started:
                        raise UnsupportedCapability(
                            ("host_cancel_root_identity",), self.codec.profile
                        )
                    self._root = root

                async def deliver(key: str) -> CancelReceipt:
                    return await self._backend.events.cancel(
                        self._scope, self._session, turn_id=self._root or "", key=key
                    )

                receipt = await self._edge(
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
                    raise ScopeViolation(self._session.id, f"foreign {label} cancellation receipt")
                while True:
                    observed = await self._edge(
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
                        stopped = await self._stop_event(receipt, observed)
                        self._check_terminal(stopped)
                        await self._persistence.record(
                            self._session, stopped, source_key=self._journal_source(stopped)
                        )
                        await self._persistence.stopped(receipt.operation_id, self._session)
                        self._terminal = observed.outcome
                        return observed
                    await asyncio.sleep(
                        min(0.1, max(0, (deadline - datetime.now(UTC)).total_seconds()))
                    )
        except TimeoutError:
            raise TurnError(
                kind="interrupt_timeout",
                message=f"{label} root stop was not observed within the deadline",
            ) from None

    async def archive(self) -> None:
        # The provider has no archive primitive. Preserve that typed refusal;
        # retiring a host binding must never silently hard-delete its session.
        raise UnsupportedCapability(("session_archive",), self.codec.profile)


OpenAIBaselineLoader = Callable[
    [ResourceRef, tuple[str, ...]], Awaitable[tuple[tuple[str, ...], bool]]
]


class OpenAITurnIO(HostedTurnIO):
    codec = OPENAI
    #: OpenAI's event stream replays nothing: a turn that ended before it connected,
    #: or between a reconcile and it, is never reported there, and keep-alive frames
    #: hide the silence (probe 2026-10-23). The driver asks for the session status
    #: whenever the stream yields nothing for this long (J4 acb5cd66).
    idle_check_s: float | None = 15.0
    # 500 turns, ten at a time, stopping at the root's one observation.
    _usage_pages, _usage_page_size = 50, 10

    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: HostedPersistence,
        root_turn_id: str | None = None,
        baseline_loader: OpenAIBaselineLoader | None = None,
        spend_guard: SpendGuard | None = None,
    ) -> None:
        super().__init__(
            backend, scope, session, persistence=persistence, root_turn_id=root_turn_id
        )
        self._baseline_loader = baseline_loader
        self._spend_guard = spend_guard

    async def _history(self) -> list[Event]:
        return await self._paged_history(None, bound=50)

    async def _take_baseline(self) -> None:
        values = await self._history()
        self._prior_roots = {e.turn_id for e in values if e.turn_id is not None}
        if self._baseline_loader is not None:
            roots, resumed = await self._baseline_loader(
                self._session, tuple(sorted(self._prior_roots))
            )
            self._prior_roots = set(roots)
            self._resuming = resumed
            self._input_started = self._input_started or resumed

    def _journal_source(self, event: Event) -> str:
        return openai_state.journal_source(event)

    def _usage_found(self, values: list[UsageObservation]) -> bool:
        return bool(values)

    async def _after_record(self, event: Event) -> None:
        """Cancel the root once its priced usage passes the row's limit (host-side cap)."""
        guard = self._spend_guard
        if (
            guard is None
            or self._root is None
            or self._terminal is not None
            or event.turn_id != self._root
            or event.type == "session.turn_ended"
        ):
            return
        try:
            over = await guard.exceeded(self.replay_usage)
        except (ProviderError, TurnConnectionLost, TimeoutError) as error:
            over = guard.unread(error)
        if not over:
            return
        await self.interrupt(timeout_s=30.0)
        await guard.notify()
        from daimon.core.turn.driver import InterruptedDuringRecovery

        raise InterruptedDuringRecovery(phase="replay")

    async def _stop_event(self, receipt: CancelReceipt, observed: StopObservation) -> Event:
        return Event(
            id="openai:stop:" + receipt.operation_id,
            session_id=self._session.id,
            sequence=0,
            type="session.turn_ended",
            turn_id=receipt.turn_id,
            observed_at=observed.observed_at,
            authority="record",
            payload=TurnEndedPayload(
                root_turn_id=receipt.turn_id,
                outcome=observed.outcome or "interrupted",
                native_reason="observed_stop",
            ).model_dump(mode="json"),
            native=NativeProvenance(
                provider="openai", api_revision=SCHEMA_DATE, event_type="turn_stop_observation"
            ),
        )


SelfHostedBaselineLoader = Callable[
    [ResourceRef, tuple[str, ...], int], Awaitable[tuple[tuple[str, ...], bool, int]]
]


class SelfHostedTurnIO(HostedTurnIO):
    codec = SELFHOSTED
    _usage_pages, _usage_page_size = 5, 100

    def __init__(
        self,
        backend: ManagedAgents,
        scope: Scope,
        session: ResourceRef,
        *,
        persistence: HostedPersistence,
        root_turn_id: str | None = None,
        history_cursor: Callable[[ResourceRef], Awaitable[int]] | None = None,
        explicit_ask: Callable[[str], bool] | None = None,
        notice_loader: Callable[[ResourceRef], Awaitable[Event | None]] | None = None,
        baseline_loader: SelfHostedBaselineLoader | None = None,
    ) -> None:
        super().__init__(
            backend, scope, session, persistence=persistence, root_turn_id=root_turn_id
        )
        self._baseline_loader = baseline_loader
        self._notice_loader = notice_loader
        self._history_cursor = history_cursor
        self._explicit_ask = explicit_ask
        self._action_ids: dict[str, str] = {}
        self._after = 0

    async def _history(self) -> list[Event]:
        # A single model turn can exceed thousands of journal records. Advance
        # by cursor until exhausted; replay/interrupt own the wall-clock bound.
        return await self._paged_history(str(self._after), bound=None)

    async def _take_baseline(self) -> None:
        page = await self._edge(
            self._backend.events.list(
                self._scope, self._session, page=PageRequest(order="desc", limit=1)
            )
        )
        values = list(page.data)
        for event in values:
            self._validate(event)
        self._after = max((event.sequence for event in values), default=0)
        if self._history_cursor is not None:
            self._after = max(self._after, await self._history_cursor(self._session))
        self._prior_roots = {e.turn_id for e in values if e.turn_id is not None}
        if self._baseline_loader is not None:
            roots, resumed, self._after = await self._baseline_loader(
                self._session, tuple(sorted(self._prior_roots)), self._after
            )
            self._prior_roots = set(roots)
            self._resuming = resumed
            self._input_started = self._input_started or resumed

    def _journal_source(self, event: Event) -> str:
        return selfhosted_state.journal_source(event)

    def _root_error(self, native_reason: str | None) -> ProviderError:
        return root_failed(native_reason)

    def _learn_actions(self, payload: RequiresActionPayload) -> None:
        for action in payload.actions:
            if action.call_id:
                self._action_ids[action.call_id] = action.id

    def _display(self, event: Event) -> tuple[DisplayEvent, ...]:
        payload = event.typed_payload()
        if isinstance(payload, RequiresActionPayload):
            self._learn_actions(payload)
        result: list[DisplayEvent] = []
        for display in super()._display(event):
            if self._explicit_ask is not None and isinstance(
                display, BetaManagedAgentsAgentToolUseEvent | BetaManagedAgentsAgentMCPToolUseEvent
            ):
                qualified = (
                    display.mcp_server_name + "/" + display.name
                    if display.type == "agent.mcp_tool_use"
                    else display.name
                )
                if display.evaluated_permission != "deny":
                    # The host pauses execution to inspect safety and saved rules;
                    # this pause does not itself require a user confirmation.
                    display = display.model_copy(
                        update={
                            "evaluated_permission": "ask"
                            if self._explicit_ask(qualified)
                            else "allow"
                        }
                    )
            result.append(display)
        return tuple(result)

    async def _resolve_inputs(
        self, events: Sequence[BetaManagedAgentsEventParams], inputs: tuple[InputEvent, ...]
    ) -> tuple[InputEvent, ...] | None:
        if any(
            isinstance(item, UserToolConfirmation) and item.action_id not in self._action_ids
            for item in inputs
        ):
            async with asyncio.timeout(REPLAY_TIMEOUT_S):
                snapshot = await self._edge(
                    self._backend.events.reconcile(self._scope, self._session)
                )
                if snapshot.session != self._session:
                    raise ScopeViolation(self._session.id, "foreign SelfHosted confirmation")
                for action in snapshot.required_actions:
                    if action.call_id:
                        self._action_ids[action.call_id] = action.id
                # Completed actions have left the pending projection. Their
                # durable approval records still identify the original batch,
                # allowing the driver to recover already-settled receipts.
                for event in await self._history():
                    if (
                        event.turn_id != self._root
                        or event.thread_id is not None
                        or event.authority not in ("record", "reconciled")
                    ):
                        continue
                    payload = event.typed_payload()
                    if isinstance(payload, RequiresActionPayload):
                        self._learn_actions(payload)
        resolved: list[InputEvent] = []
        for item in inputs:
            if isinstance(item, UserToolConfirmation):
                action_id = self._action_ids.get(item.action_id)
                if action_id is None:
                    raise UnsupportedCapability(
                        ("host_origin_confirmation_identity",), self.codec.profile
                    )
                item = item.model_copy(update={"action_id": action_id})
            resolved.append(item)
        if len(resolved) > 1:
            if any(isinstance(item, UserMessage) for item in resolved):
                raise UnsupportedCapability(("host_turn_input_batch",), self.codec.profile)
            # Each approval has its own durable action identity. The harness
            # accepts one action per request, even when the UI answers a batch.
            for event in events:
                await self.send((event,))
            return None
        return tuple(resolved)

    def _expected_turn(self, new_turn: bool) -> str | None:
        if not new_turn and self._root is None:
            raise UnsupportedCapability(("host_approval_root_identity",), self.codec.profile)
        return self._root if not new_turn else None

    async def _open_source(self) -> AsyncIterator[Event]:
        return await self._edge(
            self._backend.events.open_stream(self._scope, self._session, after=str(self._after))
        )

    async def _initial_frames(self) -> list[TurnEvent]:
        notice = (
            await self._notice_loader(self._session) if self._notice_loader is not None else None
        )
        if notice is None:
            return []
        return [
            TurnEvent(display, notice)
            for display in display_batch(notice, session_id=self._session.id, codec=self.codec)
        ]

    async def _stop_event(self, receipt: CancelReceipt, observed: StopObservation) -> Event:
        stopped = next(
            (
                event
                for event in await self._history()
                if event.turn_id == receipt.turn_id and event.type == "session.turn_ended"
            ),
            None,
        )
        if stopped is None:
            raise ProviderError("upstream", retryable=True, native_code="stop_event_unavailable")
        return stopped
