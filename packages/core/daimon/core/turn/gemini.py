"""Explicit Gemini host codec over scoped ports; no credential/client discovery.

SDK-shaped records below are display compatibility values only. The original
neutral event and interaction usage are preserved alongside them. In particular
no Anthropic model-request span or meter is manufactured for Gemini.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import uuid4

import httpx
import structlog
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
from daimon.core.turn.termination import ProviderTerminalFailure, normalized_termination_reason
from mux.contracts.actions import InputEvent, UserMessage, UserToolResult
from mux.contracts.config import ConfigRevision
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    HistoryGapPayload,
    NativePart,
    RequiresActionPayload,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
    UsageObservedPayload,
    UserMessagePayload,
)
from mux.contracts.ids import ModelRef, PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import Agent, AgentSpec, EnvironmentSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import (
    FLASH_PRIMARY,
    NO_TOOLS_FUNCTION,
    AgentCatalogTransport,
    FlashAccounting,
    FlashTransport,
    GCSSecretResolver,
    GeminiManagedAgents,
    MCPSecretResolver,
    SkillBundlePublisher,
    Storage,
    Transport,
    compile_agent,
    compile_environment,
)
from mux.errors import ContinuityLost, ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.gemini import ARGON_MODEL
from mux.state.store import StateStore
from pydantic import JsonValue, TypeAdapter

PROFILE = "gemini.inline_reuse"
_DISPLAY = TypeAdapter[BetaManagedAgentsSessionEvent](BetaManagedAgentsSessionEvent)
_INPUTS = TypeAdapter(list[dict[str, JsonValue]])
log = structlog.get_logger(__name__)

#: Most a host terminal failure waits to stop the native interaction.
ABANDON_TIMEOUT_S = 10.0
ROOT_SNAPSHOT_TIMEOUT_S = 1.0
"""Bound on the local journal read that guards an uncertain send (F12)."""
#: First backoff after a failed stream open/read; doubles per consecutive
#: failure (jittered, capped at 32x). Any delivered event resets it.
STREAM_RETRY_BASE_S = 1.0


@dataclass(frozen=True)
class GeminiDeployment:
    config_digest: str
    config_revision: int
    account_scope_id: str
    agent: AgentSpec
    environment: EnvironmentSpec
    native_agent: Agent | None = None
    max_output_tokens: int | None = None


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
    catalog_transport: AgentCatalogTransport | None = None
    mcp_secrets: MCPSecretResolver | None = None
    skill_bundle_publisher: SkillBundlePublisher | None = None
    gcs_secrets: GCSSecretResolver | None = None


def _runtime_model(runtime: TurnRuntime | None) -> str:
    journal = runtime.journal if runtime is not None else None
    deployment = journal.deployment if isinstance(journal, GeminiJournal) else None
    if deployment is None:
        return FLASH_PRIMARY
    model = deployment.agent.model.id
    return model if model == ARGON_MODEL else FLASH_PRIMARY


def compose_gemini_backend(request: TurnBackendRequest) -> TurnBackend:
    # Admission/model/durable dependencies are checked before the private
    # caller-owned transport factory, which might access a credential.
    if request.profile != PROFILE or request.model != _runtime_model(request.runtime):
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
    required_model = _runtime_model(runtime)
    if config.profile != PROFILE or config.model != required_model:
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
        or deployment.agent.model != ModelRef(provider="gemini", id=required_model)
    ):
        raise UnsupportedCapability(("gemini_pinned_deployment",), PROFILE)
    if deployment.max_output_tokens is not None:
        if deployment.native_agent is not None:
            raise UnsupportedCapability(("model_interaction_resources",), PROFILE)
        compile_agent(deployment.agent, max_output_tokens=deployment.max_output_tokens)
        compile_environment(deployment.environment, model_only=True)
    supplied = runtime.transport_factory(config, scope)
    if not isinstance(supplied, GeminiTransportRuntime):
        raise UnsupportedCapability(("gemini_private_transport",), PROFILE)
    transport = FlashTransport(
        supplied.transport,
        runtime.usage_store.accounting,
        native_agent=deployment.native_agent,
        primary=ARGON_MODEL if required_model == ARGON_MODEL else FLASH_PRIMARY,
    )
    return GeminiManagedAgents(
        transport,
        storage=runtime.journal.storage,
        state_store=runtime.journal.state_store,
        account_scope_id=account_scope_id,
        model_for_interaction=transport.model_for_interaction,
        resource_catalog_enabled=supplied.catalog_transport is not None,
        resource_catalog_transport=supplied.catalog_transport,
        native_agent=deployment.native_agent,
        max_output_tokens=deployment.max_output_tokens,
        mcp_secrets=supplied.mcp_secrets,
        gcs_secrets=supplied.gcs_secrets,
        skill_bundle_publisher=supplied.skill_bundle_publisher,
    )


def compose_gemini_codec(request: TurnCodecRequest) -> GeminiTurnIO:
    if request.config is None or request.model != _runtime_model(request.runtime):
        raise UnsupportedCapability(("gemini_host_flash_primary",), PROFILE)
    return GeminiTurnIO(
        request.backend, request.scope, request.session, persistence=request.persistence
    )


def _display_part(part: object) -> dict[str, object]:
    """Host display text for a saved tool-result part; the journal keeps it native.

    Gemini returns MCP results as an object (`result` text, `call_tool_result_json`,
    `structured_content`), not text parts.
    """
    if isinstance(part, TextPart):
        return part.model_dump()
    if isinstance(part, NativePart):  # The driver's saved tool-result payload.
        result = part.payload.get("result")
        if isinstance(result, dict):
            obj = cast(dict[str, object], result)
            for key in ("result", "call_tool_result_json"):
                text = obj.get(key)
                if isinstance(text, str):
                    return {"type": "text", "text": text}
        return {"type": "text", "text": json.dumps(result, sort_keys=True, default=str)}
    raise UnsupportedCapability(("gemini_native_tool_result_display",), PROFILE)


class GeminiTurnIO:
    """One host turn, retaining the caller's durable driver/session binding."""

    #: A replay can answer the no-tools sentinel, which sends. A retried replay
    #: would skip the answer the failed one already claimed, so the driver
    #: retries only this backend's status read after a quiet stream.
    replay_may_send: bool = True

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
        # Roots that already existed when this IO's own send came back uncertain
        # (None: that send was never uncertain). History cannot tell a stale root
        # from this turn's, so none of them may become this turn's root (F12).
        self._stale_roots: frozenset[str] | None = None
        self._streams: list[_LazyStream] = []
        self._tool_ids: dict[tuple[str | None, str], str] = {}
        self._actions: dict[str, str] = {}
        # The inert no-tools sentinel (`NO_TOOLS_FUNCTION`): its calls (call id ->
        # display event id), the calls already answered, and how many answers this
        # turn sent. Kept here, not per stream, so a reconnect never answers twice.
        self._sentinel_calls: dict[str, str] = {}
        self._sentinel_answered: set[str] = set()
        self._no_tools_replies = 0
        # Consecutive stream failures without a delivered event (backoff step).
        self.stream_failures = 0

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

        # The roots this send can't be, taken before it can record its own. A tool
        # result continues the turn's known root, so only a new turn is guarded.
        before = await self._known_roots() if kind == "send" else None
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
            # Only this attempt timing out leaves nothing recorded before the
            # snapshot. A claim by an earlier or concurrent attempt of the same
            # host operation may already have recorded this turn's root, so that
            # keeps the designed reattach.
            if kind == "send" and isinstance(error.__cause__, TimeoutError):
                self._send_uncertain(before)
            raise TurnConnectionLost(
                "Gemini delivery was already claimed; replay required"
            ) from error
        if receipt.status == "outcome_unknown":
            if kind == "send":
                self._send_uncertain(before)
            raise TurnConnectionLost("Gemini input acceptance is unknown; replay before recovery")
        if receipt.status not in {"queued", "processed"} or receipt.turn_id is None:
            raise ProviderError("upstream", retryable=False, native_code="gemini_send_rejected")
        self.root, self._ready = receipt.turn_id, True
        for stream in self._streams:
            stream.ready()

    async def _known_roots(self) -> frozenset[str] | None:
        """Every root in the local journal (None: not read within the bound)."""
        try:
            async with asyncio.timeout(ROOT_SNAPSHOT_TIMEOUT_S):
                return frozenset(e.turn_id for e in await self._history() if e.turn_id)
        except Exception:  # never delay or block a send for the guard
            return None

    def _send_uncertain(self, before: frozenset[str] | None) -> None:
        """This turn's send may or may not have been accepted: no root that
        existed before it, including one an earlier stream open picked, can be
        this turn's. A root recorded since can (the designed reattach). Without
        a snapshot the turn keeps main's reattach.
        """
        if before is None:
            return
        self._stale_roots = before
        self.root = self._own_root(self.root)

    def _own_root(self, candidate: str | None) -> str | None:
        """`candidate` if it can be this turn's root, else None."""
        if candidate is None or self._stale_roots is None:
            return candidate
        if candidate in self._stale_roots:
            return None
        return candidate

    def _latest_own_root(self, records: Sequence[Event]) -> str | None:
        """The newest root that can be this turn's: a late correction to an
        earlier turn's usage never hides one accepted since."""
        return next(
            (e.turn_id for e in reversed(records) if e.turn_id and self._own_root(e.turn_id)),
            None,
        )

    def _require_own_root(self) -> None:
        if self._stale_roots is not None and self.root is None:
            log.warning(
                "turn.gemini_own_input_missing",
                session=self.session.id,
                stale_roots=len(self._stale_roots),
            )
            raise TurnConnectionLost(
                "Gemini never showed this turn's input after an uncertain send"
            )

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
            self.root = self._own_root(session.active_root_turn) or self.root
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
            display.update(
                type="agent.tool_result",
                tool_use_id=paired,
                content=[_display_part(p) for p in payload.content],
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
        if (
            event.authority not in {"record", "reconciled"}
            or event.turn_id != self.root
            or event.thread_id is not None
            or event.session_id != self.session.id
            or event.native.provider != "gemini"
        ):
            return
        payload = event.typed_payload()
        if not isinstance(payload, TurnEndedPayload) or payload.root_turn_id != self.root:
            return
        if payload.outcome in {"errored", "terminated"}:
            reason = normalized_termination_reason(event)
            assert reason is not None  # A typed authoritative root ending has a closed outcome.
            raise ProviderTerminalFailure(reason)
        if replay and payload.outcome == "interrupted":
            from daimon.core.turn.driver import InterruptedDuringRecovery

            # Preserve saved cancellation across the SDK-only replay edge.
            raise InterruptedDuringRecovery(phase="replay")

    @property
    def no_tools_answers(self) -> int:
        """How many sentinel answers this turn sent (a stream reopens after one)."""
        return self._no_tools_replies

    def no_tools(self, event: Event) -> Literal["hide", "pending"] | None:
        """How the host shows an event of the no-tools sentinel: never.

        "hide" for the sentinel's own call and result, "pending" for a pause that
        names only sentinel calls (answered by `answer_no_tools`). A pause that
        mixes a sentinel call with a real one stays a real `requires_action`: the
        host can't answer the real call, and the turn ends as it always has.
        """
        payload = event.typed_payload()
        if isinstance(payload, ToolUsePayload) and payload.tool_name == NO_TOOLS_FUNCTION:
            self._sentinel_calls[payload.call_id] = event.id
            return "hide"
        if isinstance(payload, ToolResultPayload) and payload.call_id in self._sentinel_calls:
            return "hide"
        if (
            isinstance(payload, RequiresActionPayload)
            and payload.actions
            and all((a.call_id or a.id) in self._sentinel_calls for a in payload.actions)
        ):
            return "pending"
        return None

    async def answer_no_tools(self, event: Event) -> None:
        """Answer the sentinel calls a pause names: once per turn, never twice per call.

        The model was told the sentinel is not to be called; if it is, it gets "no
        tools are available here; answer directly" and the same turn continues. A
        second batch is still answered (so the interaction is never left waiting
        on an action the next message would trip over; the failed turn's abandon
        then cancels the continuation) and ends the turn in plain words.
        """
        payload = event.typed_payload()
        if not isinstance(payload, RequiresActionPayload):
            return
        calls = [a.call_id or a.id for a in payload.actions]
        fresh = [call for call in calls if call not in self._sentinel_answered]
        if not fresh:
            return
        looping = self._no_tools_replies > 0
        self._no_tools_replies += 1
        self._sentinel_answered.update(fresh)
        log.warning(
            "turn.gemini_no_tools_loop" if looping else "turn.gemini_no_tools_called",
            session_id=self.session.id,
            calls=len(fresh),
        )
        await self.send(
            [
                cast(
                    BetaManagedAgentsEventParams,
                    {
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": self._sentinel_calls[call],
                        "content": [{"type": "text", "text": NO_TOOLS_REPLY}],
                    },
                )
                for call in fresh
            ]
        )
        if looping:
            raise ProviderError("upstream", retryable=False, native_code="gemini_no_tools_loop")

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
                    self.root = self._latest_own_root(records)
                self._require_own_root()
                if self.persistence is not None and records:
                    final = records[-1]
                    await self.persistence.record_many(
                        self.session,
                        records,
                        cursor=final.native.cursor or final.native.event_id or final.id,
                    )
                for event in records:
                    self.check_terminal(event)
                frames: list[TurnEvent] = []
                for index, event in enumerate(records):
                    frame = self.frame(event, usages)  # registers display/action ids
                    kind = self.no_tools(event)
                    if kind == "pending" and index == len(records) - 1:
                        # Still waiting on the answer (nothing after it): give it.
                        await self.answer_no_tools(event)
                    if kind is None:
                        frames.append(frame)
                return frames
        except TimeoutError as error:
            raise TurnError(kind="upstream", message="Gemini replay deadline expired") from error

    async def replay(
        self, *, timeout_s: float = REPLAY_TIMEOUT_S
    ) -> list[BetaManagedAgentsSessionEvent]:
        # Host dispatch separately requires replay_usage before send and
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
            return await self._local_usage()

    async def replay_usage_after_rate_limit(self) -> Sequence[UsageObservation]:
        """Fetch-only exception policy for an already finalized host failure."""
        async with asyncio.timeout(REPLAY_TIMEOUT_S):
            try:
                await self.backend.usage.reconcile(self.scope, self.session)
            except ProviderError as error:
                if error.category != "rate_limited" or not error.retryable:
                    raise
                log.warning(
                    "turn.usage_reconciliation_failed",
                    category=error.category,
                    error_class=type(error).__name__,
                )
                # Local reads and validation are deliberately outside the
                # native fetch handler. Their failures must still propagate.
            else:
                return await self._local_usage()
            return await self.retained_usage()

    async def retained_usage(self) -> Sequence[UsageObservation]:
        """Bounded local proof; unknown is retained, absent proof is refused."""
        async with asyncio.timeout(REPLAY_TIMEOUT_S):
            records = await self._history()
            root = self.root
            if root is None or not any(
                e.turn_id == root and e.type == "user.message" for e in records
            ):
                raise ProviderError("upstream", retryable=False, native_code="missing_usage_root")
            values = await self._local_usage()
            if not values:
                raise ProviderError(
                    "upstream", retryable=False, native_code="missing_retained_usage"
                )
            for usage in values:
                if not any(
                    e.turn_id == root
                    and isinstance(payload := e.typed_payload(), UsageObservedPayload)
                    and payload.observation_id == usage.id
                    and payload.revision == usage.revision
                    and usage.id == f"gemini:{e.native.ordering_domain}:usage"
                    for e in records
                ):
                    raise ProviderError(
                        "upstream", retryable=False, native_code="uncorrelated_retained_usage"
                    )
            return values

    async def _local_usage(self) -> Sequence[UsageObservation]:
        if self.root is None:
            records = await self._history()
            self.root = self._latest_own_root(records)
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

    async def abandon_native(self, *, timeout_s: float = ABANDON_TIMEOUT_S) -> None:
        """Stop a still-running interaction after the host gave up on the turn.

        Best effort and bounded: a host failure must not leave Gemini working
        (and spending) on a reply nobody will deliver.
        """
        root = self.root
        if root is None:
            return
        try:
            async with asyncio.timeout(timeout_s):
                projection = await self.backend.events.reconcile(self.scope, self.session)
                # Only this turn's own interaction, and only while it still runs.
                if projection.state != "running" or projection.active_root_turn != root:
                    return
                stopped = await self._interrupt(timeout_s=timeout_s, root=root)
            log.info(
                "turn.gemini_native_abandoned",
                session_id=self.session.id,
                stopped=stopped.stopped,
                outcome=stopped.outcome,
            )
        except Exception as error:
            log.warning(
                "turn.gemini_native_abandon_failed",
                session_id=self.session.id,
                error_class=type(error).__name__,
                category=getattr(error, "category", None),
                native_code=getattr(error, "native_code", None),
            )

    async def interrupt(self, *, timeout_s: float) -> StopObservation:
        try:
            return await self._interrupt(timeout_s=timeout_s)
        except TimeoutError as error:
            raise TurnError(
                kind="interrupt_timeout", message="Gemini cancellation deadline expired"
            ) from error

    async def _interrupt(self, *, timeout_s: float, root: str | None = None) -> StopObservation:
        deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
        async with asyncio.timeout(timeout_s):
            if root is None:
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


class _StreamFailure(Exception):
    def __init__(self, phase: str, error: ProviderError) -> None:
        super().__init__(phase)
        self.phase, self.error = phase, error


def _transient_stream_error(error: ProviderError) -> bool:
    """A failed SSE open/read that says nothing about the interaction itself.

    Only network failures, overload, 408 and 5xx qualify. Other statuses
    (an unmapped 4xx), status-less SDK errors, definite refusals, rate limits
    (finalized with their retry-after) and the driver's own check failures (a
    native code) end the turn.
    """
    if error.category in ("overloaded", "transient_network"):
        return True
    status = error.native_error_code or ""
    return (
        error.category == "upstream"
        and error.native_code is None
        and (status == "http_408" or status.startswith("http_5"))
    )


NO_TOOLS_REPLY = "No tools are available here; answer directly."


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
        idle = asyncio.timeout(self.timeout_s)
        try:
            async with idle:
                await self._gate.wait()
                if self._closed:
                    raise StopAsyncIteration
                if self._source is None:
                    await self._open()
                while True:
                    if self._buffer:
                        frame = self._buffer.popleft()
                    else:
                        if self._ended:
                            await self.close()
                            raise StopAsyncIteration
                        # Opened above, or reopened by `_no_tools` on a continuation.
                        assert self._source is not None
                        try:
                            event = await self._source.__anext__()
                        except ProviderError as error:
                            raise _StreamFailure("read", error) from error
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
                    if await self._no_tools(normalized):
                        continue
                    self._seen.add(normalized.id)
                    if self.io.persistence is not None:
                        await self.io.persistence.record(self.io.session, normalized)
                    self.io.check_terminal(normalized)
                    self._ended |= normalized.type == "session.turn_ended"
                    self.io.stream_failures = 0
                    return frame
        except _StreamFailure as failure:
            await self.close()
            error = failure.error
            transient = _transient_stream_error(error)
            log.warning(
                "turn.gemini_stream_error",
                session_id=self.io.session.id,
                phase=failure.phase,
                category=error.category,
                retryable=error.retryable,
                native_code=error.native_code,
                http_status=error.native_error_code,
                action="eventless_cycle" if transient else "fail",
            )
            if not transient:
                raise error from None
            # The stream only notifies; the interaction's state is read by GET.
            # Back off (bounded by the turn deadline), then let the driver check
            # status and reconnect or finalize, as for an idle stream.
            step = min(self.io.stream_failures, 5)
            self.io.stream_failures += 1
            await asyncio.sleep(random.uniform(0.5, 1.0) * STREAM_RETRY_BASE_S * 2**step)
            raise httpx.ReadTimeout(
                f"Gemini stream failed ({error.category}); checking interaction status"
            ) from None
        except TimeoutError:
            await self.close()
            if not idle.expired():
                raise
            # Gemini can stay silent past one read while the interaction
            # still runs or has already completed. That is the driver's
            # eventless cycle (status check, then reconnect or replay), never
            # the turn ceiling, which the caller's own deadline enforces.
            raise httpx.ReadTimeout(f"Gemini stream sent no event for {self.timeout_s}s") from None
        except BaseException:
            await self.close()
            raise
        finally:
            self._reader = None

    async def _open(self) -> None:
        self._root = self.io.root
        try:
            self._source = await self.io.backend.events.open_stream(self.io.scope, self.io.session)
        except ProviderError as error:
            raise _StreamFailure("open", error) from error
        if self._closed:
            source, self._source = self._source, None
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
            raise StopAsyncIteration
        answered = self.io.no_tools_answers
        frames = await self.io.replay_turn_events(reconcile=False)
        if self.io.no_tools_answers != answered:
            # The replay answered a pending sentinel call: this stream belongs to
            # the interaction that asked. Follow the continuation instead of
            # waiting out the read timeout on a stream that will never speak.
            source, self._source = self._source, None
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
            try:
                self._source = await self.io.backend.events.open_stream(
                    self.io.scope, self.io.session
                )
            except ProviderError as error:
                raise _StreamFailure("open", error) from error
            frames = await self.io.replay_turn_events(reconcile=False)
        if self._root is None and frames:
            self._root = self.io.root
        self._buffer.extend(
            f for f in frames if f.normalized and f.normalized.turn_id == self._root
        )

    async def _no_tools(self, event: Event) -> bool:
        """Consume a no-tools sentinel event (see `GeminiTurnIO.no_tools`)."""
        kind = self.io.no_tools(event)
        if kind is None:
            return False
        self._seen.add(event.id)
        if kind == "pending":
            await self.io.answer_no_tools(event)
            # The answer continues as a new interaction of the same root turn.
            source, self._source = self._source, None
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
            await self._open()
        return True

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
