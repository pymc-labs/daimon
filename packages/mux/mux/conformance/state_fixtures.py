"""State-backed probes; host billing, selection and queues remain separate seams."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime, timedelta

from mux.conformance.runner import (
    ConformanceFailure,
    Result,
    ScriptedTransport,
    SendEvidence,
    require,
)
from mux.contracts.actions import UserMessage
from mux.contracts.errors import BindingConflict, OperationConflict, ScopeViolation
from mux.contracts.events import Event, NativeProvenance, TextPart
from mux.contracts.ids import ResourceRef
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
from mux.state.journal import JournalEntry
from mux.state.lease import StaleFence
from mux.state.memory import CrashPoint, SimulatedCrash
from mux.state.operations import OperationRecord, recovery
from mux.state.store import StateStore, bind_new_slot, binding_slot
from mux.state.usage_ledger import OutboxRow

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TTL = timedelta(minutes=5)


async def refused(error: type[Exception], call: Awaitable[object], message: str) -> None:
    try:
        await call
    except error:
        return
    raise ConformanceFailure(message)


def entry(
    session: ResourceRef, source: str, type_: str, payload: dict[str, object]
) -> JournalEntry:
    return JournalEntry(
        source_key=source,
        event=Event.model_validate(
            {
                "id": source,
                "session_id": session.id,
                "sequence": 99,
                "type": type_,
                "turn_id": "root",
                "observed_at": NOW,
                "authority": "record",
                "payload": payload,
                "native": NativeProvenance(provider=session.provider, api_revision="conformance"),
            }
        ),
    )


def recovered_receipt(
    record: OperationRecord,
    after: OperationRecord,
    receipt: SendReceipt,
    key: str,
    t: ScriptedTransport,
    reconciled: Sequence[SendEvidence],
) -> None:
    ids = record.result.get("input_ids")
    require(
        receipt.operation_id == record.operation.id == after.operation.id,
        "C04: recovery changed operation identity",
    )
    if after.operation.status in ("sent", "outcome_unknown"):
        require(receipt.status == "outcome_unknown", "C04: ambiguous intent must stay unknown")
    if record.operation.status == "processed":
        require(
            isinstance(ids, list) and bool(ids),
            "C04: processed recovery requires durable input identity",
        )
    if receipt.status in ("queued", "processed"):
        allowed = ("processed",) if receipt.status == "processed" else ("accepted", "processed")

        def matches(native: SendEvidence) -> bool:
            return (
                native.key == key
                and native.receipt.status
                in (("processed",) if receipt.status == "processed" else ("queued", "processed"))
                and native.receipt.input_ids == receipt.input_ids
                and (receipt.turn_id is None or native.receipt.turn_id == receipt.turn_id)
            )

        committed_ids = after.result.get("input_ids")
        require(
            after.operation.status in allowed
            and isinstance(committed_ids, list)
            and bool(receipt.input_ids)
            and tuple(committed_ids) == receipt.input_ids
            and (receipt.turn_id is None or after.result.get("turn_id") == receipt.turn_id)
            and any(matches(native) for native in t.upstream_sends)
            and (
                (
                    record.operation.status in allowed
                    and isinstance(ids, list)
                    and tuple(ids) == receipt.input_ids
                    and (receipt.turn_id is None or record.result.get("turn_id") == receipt.turn_id)
                )
                or any(matches(native) for native in reconciled)
            ),
            "C04: acknowledged recovery requires prior durable or native reconciliation evidence",
        )
    else:
        require(
            receipt.status in ("queued", "outcome_unknown"),
            "C04: recovery must not invent successful completion",
        )


async def c03(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    if store is None:
        return Result("C03", "pending", ("a StateStore adapter is required for operation probes",))
    s = await t.arrange("C03")
    inputs = (UserMessage(content=(TextPart(text="durable input"),)),)
    before = t.mutation_count
    receipts = await asyncio.gather(
        *(ma.events.send(s.scope, s.session.ref, inputs, key="same-key") for _ in range(3))
    )
    require(t.mutation_count == before + 1, "C03: concurrent retries duplicated upstream send")
    require(
        len({r.operation_id for r in receipts}) == 1,
        "C03: retries must identify the same operation",
    )
    completed = [r for r in receipts if r.status in ("queued", "processed")]
    require(completed, "C03: scripted acknowledged send must return its receipt")
    require(completed[0].input_ids, "C03: acknowledged receipt must preserve input identity")
    persisted = await store.get_operation(s.scope, "same-key")
    require(
        persisted is not None and persisted.operation.status in ("accepted", "processed"),
        "C03: acknowledged receipt must have durable operation evidence",
    )
    require(
        persisted is not None and persisted.operation.id == completed[0].operation_id,
        "C03: receipt identity must match the persisted operation",
    )
    restarted = t.restart_store(store)
    replay = await ma.events.send(s.scope, s.session.ref, inputs, key="same-key")
    require(replay == completed[0], "C03: restart must reconstruct the existing receipt")
    require(t.mutation_count == before + 1, "C03: restart replay duplicated upstream send")
    await refused(
        OperationConflict,
        ma.events.send(
            s.scope,
            s.session.ref,
            (UserMessage(content=(TextPart(text="different input"),)),),
            key="same-key",
        ),
        "C03: reusing a key with changed content must conflict",
    )
    await refused(
        ScopeViolation,
        ma.events.send(
            s.scope.model_copy(update={"principal_id": "other-human"}),
            s.session.ref,
            inputs,
            key="same-key",
        ),
        "C03: another principal must not inherit an operation receipt",
    )
    require(t.mutation_count == before + 1, "C03: rejected replay must precede provider writes")
    t.fault("timeout_after_accept")
    unknown = await ma.events.send(s.scope, s.session.ref, inputs, key="unknown-key")
    require(unknown.status == "outcome_unknown", "C03: acceptance timeout must stay unknown")
    record = await restarted.get_operation(s.scope, "unknown-key")
    require(
        record is not None and record.operation.status in ("sent", "outcome_unknown"),
        "C03: uncertain delivery must retain durable ambiguous intent",
    )
    require(t.mutation_count == before + 2, "C03: timeout scenario must reach upstream once")
    t.restart_store(restarted)
    again = await ma.events.send(s.scope, s.session.ref, inputs, key="unknown-key")
    require(again == unknown, "C03: uncertain replay must retain the same unknown receipt")
    require(t.mutation_count == before + 2, "C03: uncertainty must never trigger blind resend")
    return Result(
        "C03", "pass", ("receipt/digest/principal replay and uncertain send survive restart",)
    )


async def c04(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    if store is None:
        return Result("C04", "pending", ("a restart/crash StateStore adapter is required",))
    s = await t.arrange("C04")
    slot = binding_slot(s.session.binding)
    cases: tuple[tuple[str, CrashPoint, str, str, int], ...] = (
        ("claim_send", "before_commit", "pending", "send", 0),
        ("claim_send", "after_commit", "sent", "reconcile", 0),
        ("advance_operation", "before_commit", "sent", "reconcile", 1),
        ("advance_operation", "after_commit", "accepted", "observe", 1),
    )
    for index, (method, point, status, action, sends) in enumerate(cases):
        store = t.restart_store(store, crash={method: point})
        key = f"crash-{index}"
        before = t.mutation_count
        await refused(
            SimulatedCrash,
            ma.events.send(
                s.scope, s.session.ref, (UserMessage(content=(TextPart(text=key),)),), key=key
            ),
            "C04: adapter must inject the requested transaction crash",
        )
        store = t.restart_store(store)
        record = await store.get_operation(s.scope, key)
        if record is None:
            raise ConformanceFailure("C04: restart lost persisted operation intent")
        allowed = (
            {("accepted", "observe"), ("processed", "done")}
            if status == "accepted"
            else {(status, action)}
        )
        require(
            (record.operation.status, recovery(record.operation)) in allowed,
            "C04: crash crossed the wrong commit boundary",
        )
        require(
            t.mutation_count == before + sends, "C04: crash changed the expected upstream effects"
        )
        observations = len(t.reconciled_sends)
        replay = await ma.events.send(
            s.scope, s.session.ref, (UserMessage(content=(TextPart(text=key),)),), key=key
        )
        require(
            replay.operation_id == record.operation.id, "C04: recovery changed operation identity"
        )
        if action == "send":
            require(
                replay.status in ("queued", "processed"),
                "C04: unclaimed intent must be safely sendable",
            )
            require(t.mutation_count == before + 1, "C04: recovered pending intent must send once")
        else:
            after = await store.get_operation(s.scope, key)
            if after is None:
                raise ConformanceFailure("C04: recovery lost durable operation evidence")
            recovered_receipt(record, after, replay, key, t, t.reconciled_sends[observations:])
            require(
                t.mutation_count == before + sends, "C04: ambiguous/accepted recovery resent input"
            )
    fence = await store.acquire_lease(slot, holder="reference", turn_id="root", now=NOW, ttl=TTL)
    for point in ("before_commit", "after_commit"):
        cursor = f"journal-{point}"
        batch = (
            entry(
                s.session.ref, cursor + "-run", "session.status_running", {"root_turn_id": "root"}
            ),
            entry(
                s.session.ref,
                cursor + "-message",
                "agent.message",
                {"item_id": cursor, "content": [{"type": "text", "text": "durable journal"}]},
            ),
        )
        old_events = tuple(await store.read_events(s.session.ref.id))
        old_projection = await store.projection(s.session.ref.id)
        expected_events = old_events + tuple(
            item.event.model_copy(update={"sequence": len(old_events) + index})
            for index, item in enumerate(batch)
        )
        store = t.restart_store(store, crash={"append_events": point})
        await refused(
            SimulatedCrash,
            store.append_events(s.session.ref, batch, fence=fence, cursor=cursor, now=NOW),
            "C04: adapter must inject the requested journal crash",
        )
        store = t.restart_store(store)
        events = tuple(await store.read_events(s.session.ref.id))
        projection = await store.projection(s.session.ref.id)
        if point == "before_commit":
            require(
                events == old_events and projection == old_projection,
                "C04: failed append leaked partial state",
            )
        else:
            require(
                events == expected_events,
                "C04: committed journal entries must survive restart exactly",
            )
            require(
                projection is not None
                and projection.cursor == cursor
                and projection.state == "running"
                and projection.active_root_turn == "root",
                "C04: journal, projection and cursor must commit atomically",
            )
        replayed = await store.append_events(
            s.session.ref, batch, fence=fence, cursor=cursor, now=NOW
        )
        require(
            replayed.duplicates == (2 if point == "after_commit" else 0),
            "C04: recovered overlap must deduplicate",
        )
        stored = await store.read_events(s.session.ref.id)
        require(tuple(stored) == expected_events, "C04: recovery changed durable journal content")
        require(
            [e.sequence for e in stored] == list(range(len(old_events) + 2)),
            "C04: recovery must preserve unique contiguous journal order",
        )
    await store.begin_operation(
        s.scope, key="stale", request_digest="stale", operation_id="stale", now=NOW, slot=slot
    )
    later = NOW + TTL + timedelta(seconds=1)
    uncertain_keys = ("crash-1", "crash-2", "crash-3")
    uncertain = tuple([await store.get_operation(s.scope, key) for key in uncertain_keys])
    successor = await store.acquire_lease(
        slot, holder="successor", turn_id="root", now=later, ttl=TTL
    )
    require(
        successor.took_over and successor.fence > fence.fence,
        "C04: takeover must raise the lease fence",
    )
    require(
        tuple([await store.get_operation(s.scope, key) for key in uncertain_keys]) == uncertain,
        "C04: lease takeover must preserve earlier uncertain/accepted intent",
    )
    writes = t.mutation_count
    for key, persisted in zip(uncertain_keys, uncertain, strict=True):
        if persisted is None:
            raise ConformanceFailure("C04: takeover lost durable operation evidence")
        observations = len(t.reconciled_sends)
        replay = await ma.events.send(
            s.scope, s.session.ref, (UserMessage(content=(TextPart(text=key),)),), key=key
        )
        after = await store.get_operation(s.scope, key)
        if after is None:
            raise ConformanceFailure("C04: takeover lost durable operation evidence")
        recovered_receipt(persisted, after, replay, key, t, t.reconciled_sends[observations:])
        if key == "crash-1":
            require(
                replay.status in ("queued", "outcome_unknown")
                and not any(native.key == key for native in t.upstream_sends),
                "C04: never-sent claim must remain uncertain after takeover",
            )
    require(t.mutation_count == writes, "C04: lease takeover triggered a blind resend")
    before_events = tuple(await store.read_events(s.session.ref.id))
    before_projection = await store.projection(s.session.ref.id)
    await refused(
        StaleFence,
        store.claim_send(s.scope, "stale", now=later, fence=fence),
        "C04: stale worker claimed a send",
    )
    await refused(
        StaleFence,
        store.advance_operation(s.scope, "stale", "failed", now=later, fence=fence),
        "C04: stale worker advanced an operation",
    )
    await refused(
        StaleFence,
        store.append_events(
            s.session.ref,
            (
                entry(
                    s.session.ref,
                    "stale-entry",
                    "agent.message",
                    {"item_id": "stale", "content": []},
                ),
            ),
            fence=fence,
            cursor="stale",
            now=later,
        ),
        "C04: stale worker committed a journal append",
    )
    await refused(
        ScopeViolation,
        store.claim_send(s.scope, "stale", now=later, fence=None),
        "C04: unfenced worker claimed a send",
    )
    await refused(
        ScopeViolation,
        store.advance_operation(s.scope, "stale", "failed", now=later, fence=None),
        "C04: unfenced worker advanced an operation",
    )
    foreign = await store.acquire_lease(
        slot.model_copy(
            update={"thread": slot.thread.model_copy(update={"thread_id": "other-thread"})}
        ),
        holder="foreign",
        turn_id="root",
        now=later,
        ttl=TTL,
    )
    await refused(
        ScopeViolation,
        store.claim_send(s.scope, "stale", now=later, fence=foreign),
        "C04: foreign-slot worker claimed a send",
    )
    await refused(
        ScopeViolation,
        store.advance_operation(s.scope, "stale", "failed", now=later, fence=foreign),
        "C04: foreign-slot worker advanced an operation",
    )
    await refused(
        ScopeViolation,
        store.append_events(
            s.session.ref,
            (
                entry(
                    s.session.ref,
                    "foreign-entry",
                    "agent.message",
                    {"item_id": "foreign", "content": []},
                ),
            ),
            fence=foreign,
            cursor="foreign",
            now=later,
        ),
        "C04: foreign-slot worker committed a journal append",
    )
    account_slot = slot.model_copy(update={"account_id": "other-account"})
    await store.acquire_lease(account_slot, holder="successor", turn_id="root", now=NOW, ttl=TTL)
    account_fence = await store.acquire_lease(
        account_slot, holder="successor", turn_id="root", now=later, ttl=TTL
    )
    await refused(
        ScopeViolation,
        store.claim_send(s.scope, "stale", now=later, fence=account_fence),
        "C04: another account's lease claimed a send in the same thread",
    )
    orphan = s.session.ref.model_copy(update={"id": "unowned-session"})
    for _ in range(2):
        await refused(
            ScopeViolation,
            store.append_events(
                orphan,
                (
                    entry(
                        orphan,
                        "orphan-entry",
                        "agent.message",
                        {"item_id": "orphan", "content": []},
                    ),
                ),
                fence=successor,
                cursor="orphan",
                now=later,
            ),
            "C04: rejected append established journal ownership",
        )
    require(
        not await store.read_events(orphan.id) and await store.projection(orphan.id) is None,
        "C04: rejected unowned append changed journal or projection",
    )
    unchanged = await store.get_operation(s.scope, "stale")
    require(
        unchanged is not None and unchanged.operation.status == "pending",
        "C04: rejected stale/unfenced/foreign write changed intent",
    )
    require(
        tuple(await store.read_events(s.session.ref.id)) == before_events
        and await store.projection(s.session.ref.id) == before_projection,
        "C04: rejected stale/unfenced/foreign write changed journal or projection",
    )
    await store.claim_send(s.scope, "stale", now=later, fence=successor)
    return Result(
        "C04",
        "pass",
        ("crash recovery preserves evidence/ownership; stale, missing and foreign fences refused",),
    )


async def c07(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    if store is None:
        return Result("C07", "pending", ("a StateStore adapter is required for usage revisions",))
    s = await t.arrange("C07")
    observations = await ma.usage.reconcile(s.scope, s.session.ref)
    require(
        [(o.revision, o.output_tokens) for o in observations]
        == [(1, None), (2, 100), (3, 120), (4, 110)],
        "C07: normalized usage must preserve null and the full correction sequence",
    )
    require(
        len({o.id for o in observations}) == 1, "C07: revisions must retain observation identity"
    )
    rows: list[OutboxRow] = []
    for observation in observations:
        row = await store.record_usage(s.session.binding.id, observation)
        if row is None:
            raise ConformanceFailure("C07: new revision must create its accounting outbox row")
        rows.append(row)
    require(
        [r.deltas["output_tokens"] for r in rows] == [None, 100, 20, -10],
        "C07: null/correction deltas were changed",
    )
    require(
        [r.prior_applied_revision for r in rows] == [None, 1, 2, 3],
        "C07: deltas must name the preceding applied revision",
    )
    await refused(
        ScopeViolation,
        store.record_usage("foreign-binding", observations[0]),
        "C07: usage must remain owned by the binding of its session",
    )
    store = t.restart_store(store)
    for observation in (*observations, observations[1]):
        require(
            await store.record_usage(s.session.binding.id, observation) is None,
            "C07: stale or replayed usage produced another debit",
        )
    store = t.restart_store(store)
    unchanged = observations[-1].model_copy(update={"revision": 5})
    row = await store.record_usage(s.session.binding.id, unchanged)
    require(
        row is not None and row.deltas["output_tokens"] == 0 and row.prior_applied_revision == 4,
        "C07: stale replay rewound the latest accounted revision or token count",
    )
    if row is None:
        raise ConformanceFailure("C07: higher same-count revision must create its outbox row")
    rows.append(row)
    pending = await store.pending_outbox()
    require(
        {r.key: r for r in pending} == {r.key: r for r in rows} and len(pending) == 5,
        "C07: restart lost or duplicated accounting rows",
    )
    total = 0
    for row in pending:
        require(await store.mark_outbox_applied(row), "C07: new outbox row was not applied")
        require(not await store.mark_outbox_applied(row), "C07: outbox row was applied twice")
        total += row.deltas["output_tokens"] or 0
    require(
        total == 110 and not await store.pending_outbox(),
        "C07: final accounted units must be exactly 110",
    )
    require(
        not await t.restart_store(store).pending_outbox(),
        "C07: restart revived applied accounting rows",
    )
    return Result(
        "C07", "pass", ("null→100→120→110 yields None,+100,+20,-10 once; 110 units accounted",)
    )


async def c13(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    if store is None:
        return Result("C13", "pending", ("a StateStore adapter is required for the binding race",))
    s = await t.arrange("C13")
    first = s.session.binding
    second = first.model_copy(
        update={"id": "competing-binding", "native_refs": {"session": "competing-session"}}
    )
    slot = binding_slot(first)
    require(await store.get_binding(slot) is None, "C13: adapter must seed an unbound slot")
    writes = t.mutation_count
    winners = await asyncio.gather(bind_new_slot(store, first), bind_new_slot(store, second))
    require(
        winners[0] == winners[1] and winners[0] in (first, second),
        "C13: contenders must adopt exactly one real winning binding",
    )
    require(
        await store.get_binding(slot) == winners[0],
        "C13: adopted winner must be the persisted binding",
    )
    await refused(
        BindingConflict,
        store.put_binding(second, expected_generation=0),
        "C13: stale new-binding compare-and-swap overwrote the winner",
    )
    require(
        await t.restart_store(store).get_binding(slot) == winners[0],
        "C13: race winner must survive restart unchanged",
    )
    require(t.mutation_count == writes, "C13: binding resolution must not invent provider writes")
    return Result("C13", "pass", ("parallel unbound-slot contenders adopt one durable CAS winner",))
