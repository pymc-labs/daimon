"""A restartable in-memory `StateStore`, for tests and conformance.

`MemoryStateData` plays the database: it outlives a store, so
`store.restart()` is a process restart over the same committed state. Each
method stages its writes on a copy and commits by swapping it in, so a
`SimulatedCrash` before the commit leaves nothing behind and one after it
leaves everything. Every value is detached on the way in and on the way out
(a JSON round trip), because contract payloads can nest plain lists and
dicts that a caller could otherwise change under the store.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue

from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state import journal, operations, usage_ledger
from mux.state import lease as leases
from mux.state.journal import EntryIdentity, JournalAppend, JournalEntry
from mux.state.lease import Lease, LeaseState, Slot
from mux.state.operations import Begun, OperationRecord
from mux.state.store import (
    SESSION_REF,
    ConfigRevisionConflict,
    binding_slot,
    check_binding_successor,
)
from mux.state.usage_ledger import AppliedUsage, OutboxRow

CrashPoint = Literal["before_commit", "after_commit"]

type _ChannelKey = tuple[str, str, str]
type _SlotKey = tuple[str, str, str, str, str | None]
type _OperationKey = tuple[str, str, str]
type _UsageKey = tuple[str, str]


class SimulatedCrash(Exception):
    """The process died at a planned point."""


def _detach[M: BaseModel](model: M) -> M:
    return model.model_validate(model.model_dump(mode="json"))


def _channel_key(channel: ChannelRef) -> _ChannelKey:
    return (channel.tenant_id, channel.platform, channel.channel_id)


def _slot_key(slot: Slot) -> _SlotKey:
    return (*_channel_key(slot.thread.channel), slot.thread.thread_id, slot.account_id)


@dataclass
class MemoryStateData:
    """The committed state. Values are detached frozen contracts and tuples,
    never handed out, so a shallow copy of each table is a full snapshot."""

    config_revisions: dict[_ChannelKey, dict[int, ConfigRevision]] = field(
        default_factory=dict[_ChannelKey, dict[int, ConfigRevision]]
    )
    bindings: dict[_SlotKey, tuple[ProviderBinding, ...]] = field(
        default_factory=dict[_SlotKey, tuple[ProviderBinding, ...]]
    )
    binding_ids: dict[str, _SlotKey] = field(default_factory=dict[str, _SlotKey])
    operations: dict[_OperationKey, OperationRecord] = field(
        default_factory=dict[_OperationKey, OperationRecord]
    )
    leases: dict[_SlotKey, LeaseState] = field(default_factory=dict[_SlotKey, LeaseState])
    session_slots: dict[str, Slot] = field(default_factory=dict[str, Slot])
    events: dict[str, tuple[Event, ...]] = field(default_factory=dict[str, tuple[Event, ...]])
    event_sources: dict[str, frozenset[EntryIdentity]] = field(
        default_factory=dict[str, frozenset[EntryIdentity]]
    )
    projections: dict[str, ProjectionSnapshot] = field(
        default_factory=dict[str, ProjectionSnapshot]
    )
    usage: dict[_UsageKey, AppliedUsage] = field(default_factory=dict[_UsageKey, AppliedUsage])
    outbox: dict[tuple[str, str, int], OutboxRow] = field(
        default_factory=dict[tuple[str, str, int], OutboxRow]
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)

    def snapshot(self) -> MemoryStateData:
        copy = MemoryStateData(lock=self.lock)
        for table in dataclasses.fields(self):
            if table.name != "lock":
                setattr(copy, table.name, dict(getattr(self, table.name)))
        copy.config_revisions = {k: dict(v) for k, v in self.config_revisions.items()}
        return copy

    def commit(self, staged: MemoryStateData) -> None:
        for table in dataclasses.fields(self):
            if table.name != "lock":
                setattr(self, table.name, getattr(staged, table.name))


class MemoryStateStore:
    def __init__(
        self,
        data: MemoryStateData | None = None,
        *,
        crash: Mapping[str, CrashPoint] | None = None,
    ) -> None:
        self.data = data if data is not None else MemoryStateData()
        self._crash = dict(crash or {})

    def restart(self, *, crash: Mapping[str, CrashPoint] | None = None) -> MemoryStateStore:
        """A new process over the same committed state."""
        return MemoryStateStore(self.data, crash=crash)

    def _commit(self, method: str, staged: MemoryStateData) -> None:
        point = self._crash.get(method)
        if point == "before_commit":
            raise SimulatedCrash(method)
        self.data.commit(staged)
        if point == "after_commit":
            raise SimulatedCrash(method)

    # Config revisions

    async def put_config_revision(self, revision: ConfigRevision) -> ConfigRevision:
        async with self.data.lock:
            staged = self.data.snapshot()
            revisions = staged.config_revisions.setdefault(_channel_key(revision.channel), {})
            existing = revisions.get(revision.local)
            if existing is not None:
                if existing != revision:
                    raise ConfigRevisionConflict(revision.channel.channel_id, revision.local)
                return _detach(existing)
            revisions[revision.local] = _detach(revision)
            self._commit("put_config_revision", staged)
            return _detach(revision)

    async def get_config_revision(self, channel: ChannelRef, local: int) -> ConfigRevision | None:
        revision = self.data.config_revisions.get(_channel_key(channel), {}).get(local)
        return _detach(revision) if revision else None

    async def latest_config_revision(self, channel: ChannelRef) -> ConfigRevision | None:
        revisions = self.data.config_revisions.get(_channel_key(channel), {})
        return _detach(revisions[max(revisions)]) if revisions else None

    # Bindings

    async def get_binding(self, slot: Slot) -> ProviderBinding | None:
        history = self.data.bindings.get(_slot_key(slot), ())
        return _detach(history[-1]) if history else None

    async def put_binding(
        self, binding: ProviderBinding, *, expected_generation: int
    ) -> ProviderBinding:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _slot_key(binding_slot(binding))
            history = staged.bindings.get(key, ())
            check_binding_successor(
                history[-1] if history else None, binding, expected_generation=expected_generation
            )
            if staged.binding_ids.get(binding.id, key) != key:
                raise ValueError(f"binding id {binding.id} already names another slot")
            slot = binding_slot(binding)
            session_id = binding.native_refs.get(SESSION_REF)
            if session_id is not None:
                if staged.session_slots.get(session_id, slot) != slot:
                    raise ValueError(f"session {session_id} already belongs to another slot")
                staged.session_slots[session_id] = slot
            staged.bindings[key] = (*history, _detach(binding))
            staged.binding_ids[binding.id] = key
            self._commit("put_binding", staged)
            return _detach(binding)

    # Operations

    def _owned(self, staged: MemoryStateData, scope: Scope, key: str) -> OperationRecord:
        record = staged.operations.get((*operations.operation_scope(scope), key))
        if record is None:
            raise KeyError(f"no operation {key!r}")
        operations.check_owner(record, scope)
        return record

    async def begin_operation(
        self,
        scope: Scope,
        *,
        key: str,
        request_digest: str,
        operation_id: str,
        now: datetime,
        slot: Slot | None = None,
    ) -> Begun:
        async with self.data.lock:
            staged = self.data.snapshot()
            op_key = (*operations.operation_scope(scope), key)
            begun = operations.begin(
                staged.operations.get(op_key),
                scope,
                key=key,
                request_digest=request_digest,
                operation_id=operation_id,
                now=now,
                slot=slot,
            )
            if begun.fresh:
                staged.operations[op_key] = _detach(begun.record)
                self._commit("begin_operation", staged)
            return _detach(begun)

    async def get_operation(self, scope: Scope, key: str) -> OperationRecord | None:
        record = self.data.operations.get((*operations.operation_scope(scope), key))
        if record is None:
            return None
        operations.check_owner(record, scope)
        return _detach(record)

    def _fenced(
        self, staged: MemoryStateData, target: Slot | None, fence: Lease | None, now: datetime
    ) -> None:
        state = staged.leases.get(_slot_key(target)) if target else None
        leases.check_target(state, target, fence, now)

    async def claim_send(
        self, scope: Scope, key: str, *, now: datetime, fence: Lease | None
    ) -> OperationRecord:
        async with self.data.lock:
            staged = self.data.snapshot()
            record = self._owned(staged, scope, key)
            self._fenced(staged, record.slot, fence, now)
            claimed = _detach(operations.claim(record, now=now))
            staged.operations[(*operations.operation_scope(scope), key)] = claimed
            self._commit("claim_send", staged)
            return _detach(claimed)

    async def advance_operation(
        self,
        scope: Scope,
        key: str,
        status: OperationStatus,
        *,
        now: datetime,
        fence: Lease | None,
        resource: ResourceRef | None = None,
        result: Mapping[str, JsonValue] | None = None,
    ) -> OperationRecord:
        async with self.data.lock:
            staged = self.data.snapshot()
            record = self._owned(staged, scope, key)
            self._fenced(staged, record.slot, fence, now)
            advanced = _detach(
                operations.advance(record, status, now=now, resource=resource, result=result)
            )
            staged.operations[(*operations.operation_scope(scope), key)] = advanced
            self._commit("advance_operation", staged)
            return _detach(advanced)

    # Leases

    async def acquire_lease(
        self, slot: Slot, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _slot_key(slot)
            state, acquired = leases.acquire(
                staged.leases.get(key), slot, holder=holder, turn_id=turn_id, now=now, ttl=ttl
            )
            staged.leases[key] = _detach(state)
            self._commit("acquire_lease", staged)
            return _detach(acquired)

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _slot_key(lease.slot)
            state, renewed = leases.renew(staged.leases.get(key), lease, now=now, ttl=ttl)
            staged.leases[key] = _detach(state)
            self._commit("renew_lease", staged)
            return _detach(renewed)

    async def release_lease(self, lease: Lease) -> None:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _slot_key(lease.slot)
            staged.leases[key] = _detach(leases.release(staged.leases.get(key), lease))
            self._commit("release_lease", staged)

    # Journal

    async def append_events(
        self,
        session: ResourceRef,
        entries: Sequence[JournalEntry],
        *,
        fence: Lease,
        cursor: str,
        now: datetime,
    ) -> JournalAppend:
        async with self.data.lock:
            staged = self.data.snapshot()
            owner = staged.session_slots.get(session.id)
            if owner is None:
                raise ScopeViolation(session.id, "no binding names this session")
            self._fenced(staged, owner, fence, now)
            existing = staged.events.get(session.id, ())
            planned, duplicates = journal.plan_append(
                session.id,
                staged.event_sources.get(session.id, frozenset()),
                len(existing),
                [_detach(entry) for entry in entries],
            )
            appended = tuple(event for _, event in planned)
            projection = _detach(
                journal.project_all(
                    staged.projections.get(session.id) or journal.empty_projection(session, now),
                    appended,
                    cursor=cursor,
                    now=now,
                )
            )
            staged.events[session.id] = (*existing, *appended)
            staged.event_sources[session.id] = staged.event_sources.get(session.id, frozenset()) | {
                entry.identity for entry, _ in planned
            }
            staged.projections[session.id] = projection
            self._commit("append_events", staged)
            return _detach(
                JournalAppend(appended=appended, duplicates=duplicates, projection=projection)
            )

    async def read_events(
        self, session_id: str, *, after: int = -1, limit: int = 1000
    ) -> Sequence[Event]:
        events = self.data.events.get(session_id, ())
        return [_detach(event) for event in events[after + 1 : after + 1 + limit]]

    async def projection(self, session_id: str) -> ProjectionSnapshot | None:
        snapshot = self.data.projections.get(session_id)
        return _detach(snapshot) if snapshot else None

    # Usage

    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = (binding_id, observation.id)
            applied = usage_ledger.apply_observation(
                staged.usage.get(key), binding_id, _detach(observation)
            )
            if applied is None:
                return None
            usage, row = applied
            staged.usage[key] = usage
            staged.outbox[row.key] = row
            self._commit("record_usage", staged)
            return _detach(row)

    async def pending_outbox(self, *, limit: int = 100) -> Sequence[OutboxRow]:
        rows = [row for row in self.data.outbox.values() if not row.applied][:limit]
        return [_detach(row) for row in rows]

    async def mark_outbox_applied(self, row: OutboxRow) -> bool:
        async with self.data.lock:
            staged = self.data.snapshot()
            current = staged.outbox.get(row.key)
            if current is None:
                raise KeyError(f"no outbox row {row.key}")
            if current.applied:
                return False
            staged.outbox[row.key] = current.model_copy(update={"applied": True})
            self._commit("mark_outbox_applied", staged)
            return True
