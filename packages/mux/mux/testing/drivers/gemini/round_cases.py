"""Explicit live-round scenarios; importing does not allocate a client or read a key.

The financial/GO entry point owns transport creation. These functions consume
only that admitted transport, and never replace native responses or projections.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Protocol

from pydantic import JsonValue

from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.events import Event, TextPart, TurnEndedPayload
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import AgentSpec, EnvironmentSpec, Session, SessionFilter, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import Object, Transport, close_iterator
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.state.journal import JournalEntry
from mux.state.lease import StaleFence
from mux.state.memory import CrashPoint, MemoryStateStore, SimulatedCrash
from mux.state.operations import OperationRecord, request_digest
from mux.state.store import binding_slot
from mux.testing.conformance.runner import Result, require
from mux.testing.drivers.gemini.conformance_hooks import recover_send
from mux.testing.drivers.gemini.fake import MemoryStorage

MODEL = "gemini-3.8-flash"
INPUT_MAX = 1_048_576
OUTPUT_MAX = 65_536  # Thinking is a subset of this output ceiling.
MODEL_LIMITS_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash"
THINKING_SOURCE = "https://ai.google.dev/gemini-api/docs/thinking"
PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
ENVIRONMENT_SOURCE = "https://ai.google.dev/gemini-api/docs/agent-environment"
INTERACTION_CEILING_USD = Decimal("1.032192")


def case_ceiling(interactions: int) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return INTERACTION_CEILING_USD * interactions


class CaseWire(Transport, Protocol):
    """Admitted source; scopes are bound before any native interaction."""

    def bind_session(self, session: ResourceRef) -> None: ...

    def attach_driver(self, driver: GeminiManagedAgents, session: ResourceRef) -> None: ...

    @property
    def native_posts(self) -> int: ...

    @property
    def cancel_posts(self) -> int: ...

    def record(self, events: tuple[Event, ...]) -> None: ...

    def verify_receipt(
        self, session: ResourceRef, receipt: SendReceipt, inputs: Sequence[InputEvent]
    ) -> bool: ...


@dataclass(frozen=True)
class PreparedScenario:
    driver: GeminiManagedAgents
    storage: MemoryStorage
    store: MemoryStateStore
    session: Session
    spec: SessionSpec


async def prepare(
    source: CaseWire,
    scope: Scope,
    *,
    case: str,
    name: str,
    transport: Transport | None = None,
    store: MemoryStateStore | None = None,
) -> PreparedScenario:
    """Fresh explicitly scoped local resources; native creation is events.send."""
    storage = MemoryStorage()
    store = store or MemoryStateStore()
    ma = GeminiManagedAgents(
        transport or source,
        storage=storage,
        state_store=store,
        account_scope_id=scope.account_id,
    )
    agent = await ma.agents.create(
        scope,
        AgentSpec(name=name, model=ModelRef(provider="gemini", id=MODEL), tools=()),
        key=f"{name}:agent",
    )
    environment = await ma.environments.create(
        scope, EnvironmentSpec(name=name), key=f"{name}:environment"
    )
    spec = SessionSpec(
        agent=agent.ref,
        agent_revision=agent.revision,
        environment=environment.ref,
        config_revision=1,
        extensions={
            "gemini.session": ExtensionConfig(
                namespace="gemini.session",
                version=1,
                value={
                    "binding_id": f"{case}:{name}",
                    "thread": {
                        "channel": {
                            "tenant_id": scope.tenant_id,
                            "platform": "conformance",
                            "channel_id": case,
                        },
                        "thread_id": name,
                    },
                },
            )
        },
    )
    session = await ma.sessions.create(scope, spec, key=f"{name}:session")
    source.bind_session(session.ref)
    source.attach_driver(ma, session.ref)
    return PreparedScenario(ma, storage, store, session, spec)


class DeliveryBarrier:
    """Controlled client delivery fault; never synthesize a native snapshot.

    A real SDK stream is opened and closed before delivering its records. GETs
    are withheld before dispatch until the test releases the barrier. Neither
    EOF nor a held GET can serve as a provider stop observation.
    """

    def __init__(self, source: Transport) -> None:
        self.source = source
        self.release = asyncio.Event()
        self.cut_stream = False
        self.closed_streams = 0
        self.blocked_gets = 0

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        return await self.source.create(request)

    async def get(self, interaction_id: str) -> Object:
        if not self.release.is_set():
            self.blocked_gets += 1
            await self.release.wait()
        return await self.source.get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        return await self.source.cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await self.source.download_snapshot(environment_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        source = await self.source.open_stream(interaction_id, after=after)
        if not self.cut_stream:
            return source
        await close_iterator(source)
        self.closed_streams += 1

        async def eof() -> AsyncIterator[Object]:
            if False:
                yield {}  # pragma: no cover

        return eof()


async def run_c06(source: CaseWire, scope: Scope) -> Result:
    """One actual send; cancel receipt/EOF must retain occupancy until native stop.

    The fixture's constant 'root' is replaced only by the independently accepted
    native identity. All original occupancy, receipt and stop comparisons stay.
    A provider completing before cancellation is a failed one-shot scenario,
    never a second attempt or a fabricated interrupted outcome.
    """
    barrier = DeliveryBarrier(source)
    prepared = await prepare(source, scope, case="C06", name="c06", transport=barrier)
    ma, session = prepared.driver, prepared.session
    inputs = (
        UserMessage(
            content=(
                TextPart(
                    text="Without tools, reason carefully about prime numbers and count "
                    "the primes up to one million. Explain your derivation."
                ),
            )
        ),
    )
    receipt = await ma.events.send(scope, session.ref, inputs, key="c06:send")
    require(
        receipt.status == "queued"
        and len(receipt.input_ids) == 1
        and receipt.turn_id == receipt.input_ids[0],
        "C06: original acceptance must name its native input and root",
    )
    require(
        getattr(receipt, "session", session.ref) == session.ref
        and source.verify_receipt(session.ref, receipt, inputs),
        "C06: receipt must match independently captured scoped native input bytes",
    )
    root = receipt.input_ids[0]
    cancel = await ma.events.cancel(scope, session.ref, turn_id=root, key="c06:cancel")
    require(
        cancel.status == "requested" and cancel.session == session.ref and cancel.turn_id == root,
        "C06: cancellation must acknowledge only its accepted scoped root",
    )
    require(source.native_posts == 1 and source.cancel_posts == 1, "C06: one send and one cancel")
    barrier.cut_stream = True
    ended = [event async for event in ma.events.stream(scope, session.ref)]
    require(not ended and barrier.closed_streams == 1, "C06: controlled client EOF must be empty")
    stop = await ma.events.wait_stopped(
        scope, cancel, deadline=datetime.now(UTC) + timedelta(milliseconds=50)
    )
    require(
        not stop.stopped and stop.outcome is None and barrier.blocked_gets == 1,
        "C06: withheld canonical GET/EOF must not invent observed termination",
    )
    # Local listing avoids a native GET through the barrier. No projection is edited.
    occupied = await ma.sessions.list(scope, filters=SessionFilter(), page=PageRequest())
    require(
        len(occupied.data) == 1
        and occupied.data[0].ref == session.ref
        and occupied.data[0].state == "running"
        and occupied.data[0].active_root_turn == root,
        "C06: requested cancellation and EOF must retain the original occupancy",
    )
    barrier.release.set()
    for _ in range(60):
        stop = await ma.events.wait_stopped(
            scope, cancel, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
        if stop.stopped:
            break
        await asyncio.sleep(0.1)
    require(
        stop.stopped
        and stop.outcome == "interrupted"
        and stop.receipt_operation_id == cancel.operation_id,
        "C06: an actual native cancelled snapshot must precede release",
    )
    final = await ma.sessions.list(scope, filters=SessionFilter(), page=PageRequest())
    require(
        len(final.data) == 1
        and final.data[0].ref == session.ref
        and final.data[0].state == "idle"
        and final.data[0].active_root_turn is None,
        "C06: observed cancellation must release occupancy",
    )
    events = await ma.events.list(scope, session.ref, page=PageRequest())
    outcomes = [e for e in events.data if e.type == "session.turn_ended"]
    payload = outcomes[0].typed_payload() if len(outcomes) == 1 else None
    require(
        len(outcomes) == 1
        and isinstance(payload, TurnEndedPayload)
        and payload.root_turn_id == root
        and payload.outcome == "interrupted"
        and outcomes[0].session_id == session.ref.id
        and outcomes[0].turn_id == root
        and outcomes[0].thread_id is None
        and outcomes[0].authority in ("record", "reconciled")
        and outcomes[0].native.provider == "gemini",
        "C06: journal must retain exactly one actual interrupted native root",
    )
    source.record(tuple(events.data))
    return Result(
        "C06",
        "pass",
        ("actual cancel/GET; client EOF retained occupancy; canonical cancelled root released it",),
    )


def validate_replay(
    source: CaseWire,
    scope: Scope,
    session: ResourceRef,
    inputs: Sequence[InputEvent],
    record: OperationRecord,
    receipt: SendReceipt,
) -> None:
    """Same rule for every recovery/replay, including after lease takeover.

    SendReceipt has no session field. Session ownership is proved by immutable
    intent digest, committed resource (when acknowledged) and actual POST join.
    A corrupted/additive session attribute also refuses; no schema is changed.
    """
    digest = request_digest(
        {
            "session": session.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in inputs],
            "expected_turn": None,
        }
    )
    require(
        (record.tenant_id, record.account_id, record.principal_id)
        == (scope.tenant_id, scope.account_id, scope.principal_id)
        and record.operation.request_digest == digest
        and receipt.operation_id == record.operation.id
        and getattr(receipt, "session", session) == session
        and record.operation.resource in (None, session),
        "C04: every returned receipt must retain its exact owned operation/session intent",
    )
    if record.operation.status in ("sent", "outcome_unknown"):
        require(
            receipt.status == "outcome_unknown"
            and not receipt.input_ids
            and receipt.turn_id is None,
            "C04: ambiguous intent must not invent a queued/processed native acknowledgement",
        )
    else:
        require(
            record.operation.status in ("accepted", "processed")
            and record.operation.resource == session
            and receipt.status
            == ("processed" if record.operation.status == "processed" else "queued")
            and record.result.get("input_ids") == list(receipt.input_ids)
            and record.result.get("turn_id") == receipt.turn_id
            and source.verify_receipt(session, receipt, inputs),
            "C04: acknowledgement must match durable intent and independent native input/root",
        )


async def run_c04(source: CaseWire, scope: Scope) -> Result:
    """Four independently scoped crash boundaries, at most three native sends.

    Gemini cannot send another operation on an uncertain occupied session.
    Each boundary therefore uses its own binding, with one shared committed
    scenario store. Recovery never changes identity, edits provider evidence or
    manufactures lookup by an operation key. StateStore restart is the declared
    conformance transaction simulation, not deployed process durability.
    """
    cases: tuple[tuple[str, CrashPoint, str, int], ...] = (
        ("claim_send", "before_commit", "pending", 0),
        ("claim_send", "after_commit", "sent", 0),
        ("advance_operation", "before_commit", "sent", 1),
        ("advance_operation", "after_commit", "accepted", 1),
    )
    store = MemoryStateStore()
    recovered: list[tuple[PreparedScenario, str, tuple[UserMessage, ...]]] = []
    for index, (method, point, expected_status, sends) in enumerate(cases):
        prepared = await prepare(source, scope, case="C04", name=f"c04-{index}", store=store)
        slot = binding_slot(prepared.session.binding)
        await store.put_binding(prepared.session.binding, expected_generation=0)
        store = store.restart(crash={method: point})
        ma = GeminiManagedAgents(
            source, storage=prepared.storage, state_store=store, account_scope_id=scope.account_id
        )
        inputs = (UserMessage(content=(TextPart(text=f"Reply C04_OK for boundary {index}."),)),)
        key, before = f"c04:crash:{index}", source.native_posts
        try:
            await ma.events.send(scope, prepared.session.ref, inputs, key=key)
        except SimulatedCrash:
            pass
        else:
            require(False, "C04: the requested transaction death must occur")
        store = store.restart()
        ma = GeminiManagedAgents(
            source, storage=prepared.storage, state_store=store, account_scope_id=scope.account_id
        )
        record = await store.get_operation(scope, key)
        require(record is not None, "C04: original durable intent must survive restart")
        if record is None:
            raise ValueError("unreachable missing operation")
        require(
            record.operation.status == expected_status and source.native_posts == before + sends,
            "C04: native effects must match the independent commit boundary",
        )
        if expected_status == "pending":
            try:
                await recover_send(ma, store, scope, prepared.session.ref, inputs, key=key)
            except UnsupportedCapability:
                pass
            else:
                require(False, "C04: unclaimed intent requires a separate explicit send decision")
            replay = await ma.events.send(scope, prepared.session.ref, inputs, key=key)
            require(
                replay.operation_id == record.operation.id
                and replay.status == "queued"
                and len(replay.input_ids) == 1
                and source.native_posts == before + 1,
                "C04: original unclaimed intent must send once without identity change",
            )
            require(
                source.verify_receipt(prepared.session.ref, replay, inputs),
                "C04: unclaimed send must match independently captured native acceptance",
            )
        else:
            evidence = await recover_send(ma, store, scope, prepared.session.ref, inputs, key=key)
            validate_replay(source, scope, prepared.session.ref, inputs, record, evidence.receipt)
            require(
                evidence.receipt.operation_id == record.operation.id
                and await store.get_operation(scope, key) == record
                and source.native_posts == before + sends,
                "C04: recovery must preserve committed intent and never resend",
            )
            if expected_status == "accepted":
                require(
                    evidence.basis == "durable_acknowledgement"
                    and evidence.receipt.status == "queued"
                    and bool(evidence.receipt.input_ids)
                    and evidence.receipt.turn_id is not None,
                    "C04: accepted acknowledgement is not processed completion",
                )
                require(
                    source.verify_receipt(prepared.session.ref, evidence.receipt, inputs)
                    and record.result.get("input_ids") == list(evidence.receipt.input_ids)
                    and record.result.get("turn_id") == evidence.receipt.turn_id,
                    "C04: durable acknowledgement must match real scoped native input/root bytes",
                )
            else:
                require(
                    evidence.basis == "native_lookup_unavailable"
                    and evidence.receipt.status == "outcome_unknown"
                    and not evidence.receipt.input_ids
                    and evidence.receipt.turn_id is None,
                    "C04: Gemini's unavailable native key lookup must remain explicitly unknown",
                )
        # Replay the SAME immutable operation, never create a retry attempt.
        before_replay = source.native_posts
        original = await ma.events.send(scope, prepared.session.ref, inputs, key=key)
        persisted = await store.get_operation(scope, key)
        require(persisted is not None, "C04: replay lost durable intent")
        if persisted is None:
            raise ValueError("unreachable missing operation")
        validate_replay(source, scope, prepared.session.ref, inputs, persisted, original)
        again = await ma.events.send(scope, prepared.session.ref, inputs, key=key)
        validate_replay(source, scope, prepared.session.ref, inputs, persisted, again)
        require(
            again == original and source.native_posts == before_replay,
            "C04: acknowledged/ambiguous replay must never add a native send",
        )
        journal = await ma.events.list(scope, prepared.session.ref, page=PageRequest())
        source.record(tuple(journal.data))
        recovered.append((prepared, key, inputs))
        # Every boundary also proves local journal death/overlap/fence against
        # its OWN binding; no foreign or uncertain binding is reassigned.
        await journal_fences(store, scope, prepared.session)
        require(await store.get_binding(slot) == prepared.session.binding, "C04: binding changed")
    before = source.native_posts
    for prepared, key, inputs in recovered:
        ma = GeminiManagedAgents(
            source, storage=prepared.storage, state_store=store, account_scope_id=scope.account_id
        )
        record = await store.get_operation(scope, key)
        evidence = await recover_send(ma, store, scope, prepared.session.ref, inputs, key=key)
        require(record is not None, "C04: takeover lost durable intent")
        if record is None:
            raise ValueError("unreachable missing operation")
        validate_replay(source, scope, prepared.session.ref, inputs, record, evidence.receipt)
        require(
            await store.get_operation(scope, key) == record,
            "C04: lease takeover/recovery must preserve prior intent",
        )
    require(source.native_posts == before == 3, "C04: exactly three bounded native interactions")
    return Result(
        "C04",
        "pass",
        ("four actual send-claim/ack boundaries; typed unknown recovery; journal/fence probes",),
    )


async def journal_fences(store: MemoryStateStore, scope: Scope, session: Session) -> None:
    """The original local transaction checks; no provider text/outcome is forged."""
    from mux.testing.conformance.state_fixtures import entry, refused

    clock, ttl = datetime.now(UTC), timedelta(minutes=5)
    slot = binding_slot(session.binding)
    fence = await store.acquire_lease(
        slot, holder="round-original", turn_id="root", now=clock, ttl=ttl
    )
    for point in ("before_commit", "after_commit"):
        cursor = f"local-journal-{point}"
        batch = (
            entry(session.ref, cursor + "-run", "session.status_running", {"root_turn_id": "root"}),
            entry(
                session.ref,
                cursor + "-message",
                "agent.message",
                {"item_id": cursor, "content": [{"type": "text", "text": "durable journal"}]},
            ),
        )
        before, projection = (
            tuple(await store.read_events(session.ref.id)),
            await store.projection(session.ref.id),
        )
        expected = before + tuple(
            item.event.model_copy(update={"sequence": len(before) + index})
            for index, item in enumerate(batch)
        )
        crashed = store.restart(crash={"append_events": point})
        await refused(
            SimulatedCrash,
            crashed.append_events(session.ref, batch, fence=fence, cursor=cursor, now=clock),
            "C04: original local journal commit boundary must be exercised",
        )
        restarted = crashed.restart()
        require(
            tuple(await restarted.read_events(session.ref.id))
            == (before if point == "before_commit" else expected),
            "C04: local journal must be atomic across death",
        )
        after = await restarted.projection(session.ref.id)
        if point == "before_commit":
            require(after == projection, "C04: uncommitted projection leaked")
        else:
            require(
                after is not None
                and after.cursor == cursor
                and after.state == "running"
                and after.active_root_turn == "root",
                "C04: cursor/projection/journal must commit together",
            )
        overlap = await restarted.append_events(
            session.ref, batch, fence=fence, cursor=cursor, now=clock
        )
        require(
            overlap.duplicates == (0 if point == "before_commit" else 2),
            "C04: overlap must deduplicate",
        )
        require(
            tuple(await restarted.read_events(session.ref.id)) == expected,
            "C04: restart changed journal bytes/order",
        )
        require(
            [e.sequence for e in expected] == list(range(len(expected))),
            "C04: journal sequence must be contiguous",
        )
    later = clock + ttl + timedelta(seconds=1)
    successor = await store.acquire_lease(
        slot, holder="round-successor", turn_id="root", now=later, ttl=ttl
    )
    require(
        successor.took_over and successor.fence > fence.fence,
        "C04: lease takeover must raise fence",
    )
    key = f"local-stale:{session.ref.id}"
    await store.begin_operation(
        scope, key=key, request_digest=key, operation_id=key, now=later, slot=slot
    )
    before_events, before_projection = (
        tuple(await store.read_events(session.ref.id)),
        await store.projection(session.ref.id),
    )
    await refused(
        StaleFence,
        store.claim_send(scope, key, now=later, fence=fence),
        "C04: stale send claim must refuse",
    )
    await refused(
        StaleFence,
        store.advance_operation(scope, key, "failed", now=later, fence=fence),
        "C04: stale operation write must refuse",
    )
    stale = JournalEntry(
        source_key="stale",
        event=entry(
            session.ref, "stale", "agent.message", {"item_id": "stale", "content": []}
        ).event,
    )
    await refused(
        StaleFence,
        store.append_events(session.ref, (stale,), fence=fence, cursor="stale", now=later),
        "C04: stale journal write must refuse",
    )
    await refused(
        ScopeViolation,
        store.get_operation(scope.model_copy(update={"principal_id": "foreign"}), key),
        "C04: foreign operation principal must refuse",
    )
    await refused(
        ScopeViolation,
        store.claim_send(scope, key, now=later, fence=None),
        "C04: unfenced send claim must refuse",
    )
    await refused(
        ScopeViolation,
        store.advance_operation(scope, key, "failed", now=later, fence=None),
        "C04: unfenced operation write must refuse",
    )
    foreign = await store.acquire_lease(
        slot.model_copy(
            update={"thread": slot.thread.model_copy(update={"thread_id": "foreign-thread"})}
        ),
        holder="foreign",
        turn_id="root",
        now=later,
        ttl=ttl,
    )
    await refused(
        ScopeViolation,
        store.claim_send(scope, key, now=later, fence=foreign),
        "C04: foreign-slot send claim must refuse",
    )
    await refused(
        ScopeViolation,
        store.advance_operation(scope, key, "failed", now=later, fence=foreign),
        "C04: foreign-slot operation write must refuse",
    )
    await refused(
        ScopeViolation,
        store.append_events(session.ref, (stale,), fence=foreign, cursor="foreign", now=later),
        "C04: foreign-slot journal write must refuse",
    )
    account_slot = slot.model_copy(update={"account_id": "foreign-account"})
    await store.acquire_lease(account_slot, holder="successor", turn_id="root", now=clock, ttl=ttl)
    account_fence = await store.acquire_lease(
        account_slot, holder="successor", turn_id="root", now=later, ttl=ttl
    )
    await refused(
        ScopeViolation,
        store.claim_send(scope, key, now=later, fence=account_fence),
        "C04: foreign-account send claim must refuse",
    )
    orphan = session.ref.model_copy(update={"id": "unowned-session"})
    for _ in range(2):
        orphan_entry = entry(
            orphan, "orphan-entry", "agent.message", {"item_id": "orphan", "content": []}
        )
        await refused(
            ScopeViolation,
            store.append_events(
                orphan, (orphan_entry,), fence=successor, cursor="orphan", now=later
            ),
            "C04: refused unowned append must not establish ownership",
        )
    require(
        not await store.read_events(orphan.id) and await store.projection(orphan.id) is None,
        "C04: refused unowned append changed state",
    )
    unchanged = await store.get_operation(scope, key)
    require(
        unchanged is not None and unchanged.operation.status == "pending",
        "C04: refused stale/unfenced/foreign writes changed intent",
    )
    require(
        tuple(await store.read_events(session.ref.id)) == before_events
        and await store.projection(session.ref.id) == before_projection,
        "C04: rejected stale/foreign writes changed state",
    )
    await store.claim_send(scope, key, now=later, fence=successor)
