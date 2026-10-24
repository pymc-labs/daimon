"""Worker fleets: an orchestrator turn's fan-out, kept durable.

A fleet run is the orchestrator's inbox. Each worker is one more binding slot
in the neutral state store, so its session, fenced lease, idempotent spawn
operation, journal and usage are the store's existing ones. The fleet tables
(`daimon.core.stores.fleet`) hold only what the store cannot: the run and its
workers, their specs, budgets, deadlines and status, and a pointer to each
worker's result, which itself lives in the worker's journal.

Every status change is a compare-and-swap on the row's `version`, along
`WORKER_TRANSITIONS` / `RUN_TRANSITIONS`; a lost race or a refused move raises
`FleetConflict`. Concurrency is a pool of its own
(`daimon.core.stores.fleet.WorkerPool`): it caps one process, and caps a tenant
from the durable rows, so a fan-out never takes the chat turns' slots and a
restart cannot leak a slot.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal, Protocol

from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.journal import JournalEntry, is_authoritative
from mux.state.lease import Lease, Slot
from mux.state.store import StateStore
from pydantic import JsonValue

type RunStatus = Literal["running", "collecting", "completed", "cancelled", "failed"]
type WorkerStatus = Literal[
    "pending",
    "starting",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "budget_exceeded",
]

TERMINAL: Final[frozenset[WorkerStatus]] = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "budget_exceeded"}
)
"""A worker in one of these never moves again."""

WORKER_TRANSITIONS: Final[Mapping[WorkerStatus, frozenset[WorkerStatus]]] = {
    "pending": frozenset({"starting", "cancelled", "failed"}),
    # Back to pending only through recovery, for a turn that was never sent.
    "starting": frozenset({"running", "pending", "failed", "cancelled", "timed_out"}),
    "running": frozenset(
        {"succeeded", "failed", "cancelled", "timed_out", "budget_exceeded", "pending"}
    ),
}

RUN_TRANSITIONS: Final[Mapping[RunStatus, frozenset[RunStatus]]] = {
    "running": frozenset({"collecting", "cancelled", "failed"}),
    "collecting": frozenset({"completed", "cancelled", "failed"}),
}

EXCERPT_LIMIT: Final = 4096


type FleetConflictReason = Literal[
    "claim_missing",
    "claim_changed",
    "admission_claim_invalid",
    "journal_owner_mismatch",
    "source_lineage_changed",
    "intent_changed",
    "financial_admission_refused",
    "worker_limit",
    "private_slot_mismatch",
    "source_intent_changed",
    "result_journal_missing",
    "input_reconciliation_required",
    "input_claim_mismatch",
    "recovery_reconciliation_required",
    "retained_revision_changed",
    "result_not_successful",
    "wake_pending",
    "worker_setup_busy",
]


#: What a worker's closed error code means for the fleet card: the cause, then what a
#: person can do about it. Unknown codes stay codes.
WORKER_ERROR_TEXT: Mapping[str, str] = {
    # A refusal with no more specific cause: the reference finds it in the logs.
    "dispatch_refused": "Daimon refused to start it; try again, or give an admin the reference",
    "preparation_refused": "Daimon refused to set it up; try again, or give an admin the reference",
    "financial_admission_refused": (
        "it would have gone over the fleet or server budget; ask for fewer or cheaper workers"
    ),
    "not_admitted": (
        "it would have gone over the fleet or server budget; ask for fewer or cheaper workers"
    ),
    "budget_exceeded": "it used up its budget; ask for a bigger budget or a shorter task",
    "worker_budget_exceeded": "it used up its budget; ask for a bigger budget or a shorter task",
    "run_budget_exceeded": "the fleet's budget for this run ran out; ask for a bigger budget",
    "provider_budget_exceeded": (
        "the fleet's budget for that provider ran out; ask for a bigger budget or another model"
    ),
    "tenant_budget_exceeded": "this server's spending limit was reached; an admin can raise it",
    "channel_budget_exceeded": "this channel's spending limit was reached; an admin can raise it",
    "admission_channel_budget_exceeded": (
        "this channel's spending limit was reached; an admin can raise it"
    ),
    "cap_exceeded": "a spending limit was reached; an admin can raise it",
    "admission_cap_exceeded": "a spending limit was reached; an admin can raise it",
    "balance_depleted": "this server is out of credit; an admin can top it up",
    "provider_hold_cap_reached": (
        "too many replies were already running on that model for this account; "
        "try again when one finishes"
    ),
    "provider_busy": (
        "the model provider didn't answer in time (it may be busy); try again in a minute"
    ),
    # The same cause and fix as `provider_quota_copy` (a turn's notice); the card line
    # already names the model.
    "provider_quota_zero": (
        "this server's Google plan doesn't include that model; "
        "pick another model or ask an admin to upgrade it"
    ),
    "provider_rate_limited": "Google is rate-limiting that model right now; try again in a minute",
    "provider_setup_pending": "that model provider is still being set up; try again in a minute",
    "timed_out": "it ran out of time; ask again with a smaller task",
    "deadline_expired": "it ran out of time; ask again with a smaller task",
    "preparation_deadline_exceeded": "it took too long to start; try again",
    "run_deadline": "the fleet ran out of time; ask again with a smaller task",
    "worker_lost": "it was lost when Daimon restarted; ask again",
    "stop_requested": "it was stopped",
    # The budget watch's own stop reasons, when they end a worker.
    "worker_stop_requested": "it was stopped",
    "terminal": "the fleet had already ended; ask again",
    "funding_unavailable": (
        "this server's balance couldn't be checked, so it didn't start; try again in a minute"
    ),
    "pricing_unavailable": (
        "its cost couldn't be priced, so it was stopped; ask for another model, "
        "or give an admin the reference"
    ),
    "accepted_attempt_inventory_unavailable": (
        "an earlier attempt couldn't be accounted for, so it wasn't run again; "
        "give an admin the reference"
    ),
    "watch_ended": "the fleet ended before it finished; ask again",
    "shutdown": "Daimon restarted before it finished; ask again",
    "usage_unavailable": "its spending couldn't be measured, so it was stopped; try again",
    # A bug on our side, never a model limit: the card adds a reference to the logs.
    "setup_error": "a setup error on our side; give an admin the reference",
    "provider_error": (
        "the model provider returned an error or didn't respond; try again or ask for another model"
    ),
    "provider_not_configured": (
        "that model provider isn't set up on this server yet; an admin can add its key, "
        "or ask for another model"
    ),
    "provider_not_allowed": (
        "this server doesn't allow that model provider; an admin can allow it, "
        "or ask for another model"
    ),
    "provider_daily_cap_reached": (
        "today's budget for that model on this server is used up; it resets at midnight UTC, "
        "or ask for another model"
    ),
    "provider_price_unavailable": (
        "Daimon can't price that model right now, so it won't run it; ask for another model"
    ),
    "channel_backend_pinned": (
        "this channel is set to run on one model provider only; ask for a model from it, "
        "or an admin can change the channel's setting"
    ),
    "capability_unsupported": (
        "its model can't run here with something its setup needs (e.g. its environment, "
        "memory or a connected tool); ask for another model, or give an admin the reference"
    ),
    "worker_runtime_missing": (
        "this server isn't set up to run that model's workers yet; ask for another model, "
        "or give an admin the reference"
    ),
}

#: Codes whose fix is "give an admin the reference": the card adds the worker id prefix
#: the host logs (fleet.worker_task_failed).
_REFERENCED_CODES: Final = frozenset(
    {
        "setup_error",
        "dispatch_refused",
        "preparation_refused",
        "worker_runtime_missing",
        "capability_unsupported",
        "pricing_unavailable",
        "accepted_attempt_inventory_unavailable",
    }
)


def worker_error_text(code: str | None, *, ref: str | None = None) -> str | None:
    """The card's reason for a worker's error code: its cause, its fix and any log reference."""
    text = WORKER_ERROR_TEXT.get(code) if code else None
    if text is not None and code in _REFERENCED_CODES and ref:
        return f"{text} (ref {ref})"
    return text


class FleetConflict(Exception):
    """A lost `version` compare-and-swap, or a status move the transitions refuse."""

    def __init__(self, message: str, *, reason: FleetConflictReason | None = None) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class ResultPointer:
    """Where a worker's result is: one journal event, with its digest."""

    session_id: str
    sequence: int
    digest: str
    excerpt: str


@dataclass(frozen=True)
class FleetRun:
    id: uuid.UUID
    tenant_id: uuid.UUID
    orchestrator_account_id: uuid.UUID
    orchestrator_session_id: str
    orchestrator_binding_id: str
    platform: str
    channel_id: str
    thread_id: str
    status: RunStatus
    total_budget_usd: Decimal
    reserved_usd: Decimal
    observed_usd: Decimal
    deadline_at: datetime
    max_workers: int
    quorum: int | None
    version: int


@dataclass(frozen=True)
class FleetWorker:
    id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    ordinal: int
    role: str
    spec: Mapping[str, JsonValue]
    spec_digest: str
    backend: str
    profile: str
    model: str
    binding_id: str | None
    status: WorkerStatus
    budget_usd: Decimal
    reserved_usd: Decimal
    deadline_at: datetime
    max_turns: int
    turns_used: int
    attempts: int
    reviewer_of: uuid.UUID | None
    result_contract: Mapping[str, JsonValue] | None
    result: ResultPointer | None
    error_code: str | None
    version: int


def check_worker_move(current: WorkerStatus, target: WorkerStatus) -> None:
    if target not in WORKER_TRANSITIONS.get(current, frozenset()):
        raise FleetConflict(f"a {current} worker cannot become {target}")


def check_run_move(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS.get(current, frozenset()):
        raise FleetConflict(f"a {current} run cannot become {target}")


class ResultWorker(Protocol):
    """The immutable worker identity used to validate a journal result."""

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def run_id(self) -> uuid.UUID: ...

    @property
    def tenant_id(self) -> uuid.UUID: ...

    @property
    def ordinal(self) -> int: ...

    @property
    def binding_id(self) -> str | None: ...


def worker_slot(run: FleetRun, worker: ResultWorker) -> Slot:
    """The worker's private binding slot: its own thread under the orchestrator's
    channel, owned by the orchestrator's account. Never the shared slot."""
    channel = ChannelRef(
        tenant_id=str(run.tenant_id), platform=run.platform, channel_id=run.channel_id
    )
    return Slot(
        thread=ThreadRef(channel=channel, thread_id=f"fleet/{run.id}/{worker.ordinal}"),
        account_id=str(run.orchestrator_account_id),
    )


def spawn_operation_key(run_id: uuid.UUID, ordinal: int) -> str:
    """The state store operation key that makes a worker's spawn idempotent."""
    return f"fleet:{run_id}:spawn:{ordinal}"


def result_source_key(worker_id: uuid.UUID) -> str:
    """The journal source key of a worker's result entry (deduplicated per worker)."""
    return f"fleet:{worker_id}:result"


def holds_slot(worker: FleetWorker) -> bool:
    """Whether a worker counts against its run's `max_workers`.

    A cancelled worker that never bound, ran, held money or produced a result (a
    spawn refused at admission) frees its slot; its row stays as evidence. The
    store also keeps any worker with a claim stamp, which this view cannot see.
    """
    return not (
        worker.status == "cancelled"
        and worker.binding_id is None
        and worker.attempts == 0
        and worker.turns_used == 0
        and worker.reserved_usd == 0
        and worker.result is None
    )


def run_settled(run: FleetRun, workers: Sequence[FleetWorker]) -> bool:
    """Whether the orchestrator should be woken: every worker done, or its quorum."""
    done = sum(1 for worker in workers if worker.status in TERMINAL)
    if run.quorum is not None:
        return done >= run.quorum
    return bool(workers) and done == len(workers)


def result_digest(payload: Mapping[str, JsonValue]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _excerpt(payload: Mapping[str, JsonValue]) -> str:
    """The result's text (a message's text blocks), else its JSON, cut for the card."""
    text = payload.get("text")
    if isinstance(text, str):
        return text[:EXCERPT_LIMIT]
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    parts = [
        part
        for block in blocks
        if isinstance(block, dict) and isinstance(part := block.get("text"), str)
    ]
    body = "".join(parts) if parts else json.dumps(payload, sort_keys=True)
    return body[:EXCERPT_LIMIT]


def _check_member(run: FleetRun, worker: ResultWorker) -> None:
    if worker.run_id != run.id or worker.tenant_id != run.tenant_id:
        raise FleetConflict(f"worker {worker.id} is not in run {run.id}")


def check_result(
    run: FleetRun,
    worker: ResultWorker,
    binding: ProviderBinding | None,
    stored: Event | None,
    pointer: ResultPointer,
) -> None:
    """Refuse a result that is not this worker's own journal entry.

    The worker's private slot must be bound (`binding`) to the pointer's session,
    and the entry at the pointer (`stored`) must be the worker's result entry with
    the pointer's digest and excerpt. `append_result` makes such pointers;
    `record_result` checks one against the store before the worker succeeds.
    """
    _check_member(run, worker)
    if (
        binding is None
        or binding.thread != worker_slot(run, worker).thread
        or binding.native_refs.get("session") != pointer.session_id
        or (worker.binding_id is not None and binding.id != worker.binding_id)
    ):
        raise FleetConflict(f"session {pointer.session_id} is not worker {worker.id}'s")
    if (
        stored is None
        or not is_authoritative(stored)
        or stored.id != result_source_key(worker.id)
        or stored.session_id != pointer.session_id
        or stored.sequence != pointer.sequence
        or result_digest(dict(stored.payload)) != pointer.digest
        or _excerpt(dict(stored.payload)) != pointer.excerpt
    ):
        raise FleetConflict(
            f"worker {worker.id} has no result at {pointer.session_id}:{pointer.sequence}"
        )


async def append_result(
    store: StateStore,
    run: FleetRun,
    worker: FleetWorker,
    session: ResourceRef,
    event: Event,
    *,
    lease: Lease,
    cursor: str,
    now: datetime,
) -> ResultPointer:
    """Append the worker's result to its own journal, once, under its own lease.

    The lease must be the worker's slot's, the session the one its binding names,
    and the event one of that session's; anything else is refused before the
    journal is touched. The result
    entry is the worker's, not the caller's event: it is stored under an id and
    source key derived from the worker, so it never repeats an event the turn
    already journaled, and a retry or a lease successor (whatever event object it
    rebuilt) is a journal duplicate. The pointer always names that one entry.
    Only a recorded or reconciled event can be a result: a preview or a gap is
    refused before anything is written, and recovery never picks one.
    """
    if not is_authoritative(event):
        raise FleetConflict(f"a result must be a recorded event, not a {event.authority} one")
    _check_member(run, worker)
    slot = worker_slot(run, worker)
    if lease.slot != slot:
        raise FleetConflict(f"the lease is not worker {worker.id}'s slot")
    binding = await store.get_binding(slot)
    if (
        binding is None
        or binding.native_refs.get("session") != session.id
        or (worker.binding_id is not None and binding.id != worker.binding_id)
    ):
        raise FleetConflict(f"session {session.id} is not worker {worker.id}'s")
    if event.session_id != session.id:
        # The result must come from this worker's own session: never relabel
        # another session's event (another worker's answer) as this one's.
        raise FleetConflict(
            f"event {event.id} is from session {event.session_id}, not {session.id}"
        )
    result_id = result_source_key(worker.id)
    entry = event.model_copy(update={"id": result_id})
    appended = await store.append_events(
        session,
        [JournalEntry(source_key=result_id, event=entry)],
        fence=lease,
        cursor=cursor,
        now=now,
    )
    stored = appended.appended[0] if appended.appended else await _stored(store, session, result_id)
    payload = dict(stored.payload)
    pointer = ResultPointer(
        session_id=session.id,
        sequence=stored.sequence,
        digest=result_digest(payload),
        excerpt=_excerpt(payload),
    )
    check_result(run, worker, binding, stored, pointer)
    return pointer


async def _stored(store: StateStore, session: ResourceRef, event_id: str) -> Event:
    after = -1
    while True:
        page = await store.read_events(session.id, after=after)
        if not page:
            raise FleetConflict(f"result {event_id} is not in session {session.id}")
        for stored in page:
            if stored.id == event_id and is_authoritative(stored):
                return stored
        after = page[-1].sequence
