"""A restartable in-memory `StateStore`, for tests and conformance.

`MemoryStateData` plays the database: it outlives a store, so
`store.restart()` is a process restart over the same committed state. Each
method stages its writes on a copy and commits by swapping it in, so a
`SimulatedCrash` before the commit leaves nothing behind and one after it
leaves everything.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import JsonValue

from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.state import journal, operations, usage_ledger
from mux.state import lease as leases
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease, LeaseState
from mux.state.operations import Begun, OperationRecord
from mux.state.store import ConfigRevisionConflict, check_binding_successor
from mux.state.usage_ledger import AppliedUsage, OutboxRow

CrashPoint = Literal["before_commit", "after_commit"]

type _ChannelKey = tuple[str, str, str]
type _ThreadKey = tuple[str, str, str, str]
type _OperationKey = tuple[str, str, str]
type _UsageKey = tuple[str, str]


class SimulatedCrash(Exception):
    """The process died at a planned point."""


def _channel_key(channel: ChannelRef) -> _ChannelKey:
    return (channel.tenant_id, channel.platform, channel.channel_id)


def _thread_key(thread: ThreadRef) -> _ThreadKey:
    return (*_channel_key(thread.channel), thread.thread_id)


@dataclass
class MemoryStateData:
    """The committed state. Values are frozen contracts and tuples, so a
    shallow copy of each table is a full snapshot."""

    config_revisions: dict[_ChannelKey, dict[int, ConfigRevision]] = field(
        default_factory=dict[_ChannelKey, dict[int, ConfigRevision]]
    )
    bindings: dict[_ThreadKey, tuple[ProviderBinding, ...]] = field(
        default_factory=dict[_ThreadKey, tuple[ProviderBinding, ...]]
    )
    operations: dict[_OperationKey, OperationRecord] = field(
        default_factory=dict[_OperationKey, OperationRecord]
    )
    leases: dict[_ThreadKey, LeaseState] = field(default_factory=dict[_ThreadKey, LeaseState])
    events: dict[str, tuple[Event, ...]] = field(default_factory=dict[str, tuple[Event, ...]])
    event_sources: dict[str, frozenset[tuple[str, int]]] = field(
        default_factory=dict[str, frozenset[tuple[str, int]]]
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
                return existing
            revisions[revision.local] = revision
            self._commit("put_config_revision", staged)
            return revision

    async def get_config_revision(self, channel: ChannelRef, local: int) -> ConfigRevision | None:
        return self.data.config_revisions.get(_channel_key(channel), {}).get(local)

    async def latest_config_revision(self, channel: ChannelRef) -> ConfigRevision | None:
        revisions = self.data.config_revisions.get(_channel_key(channel), {})
        return revisions[max(revisions)] if revisions else None

    # Bindings

    async def get_binding(self, thread: ThreadRef) -> ProviderBinding | None:
        history = self.data.bindings.get(_thread_key(thread), ())
        return history[-1] if history else None

    async def put_binding(
        self, binding: ProviderBinding, *, expected_generation: int
    ) -> ProviderBinding:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _thread_key(binding.thread)
            history = staged.bindings.get(key, ())
            check_binding_successor(
                history[-1] if history else None, binding, expected_generation=expected_generation
            )
            staged.bindings[key] = (*history, binding)
            self._commit("put_binding", staged)
            return binding

    # Operations

    async def begin_operation(
        self, scope: Scope, *, key: str, request_digest: str, operation_id: str, now: datetime
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
            )
            if begun.fresh:
                staged.operations[op_key] = begun.record
                self._commit("begin_operation", staged)
            return begun

    async def get_operation(self, scope: Scope, key: str) -> OperationRecord | None:
        return self.data.operations.get((*operations.operation_scope(scope), key))

    async def advance_operation(
        self,
        scope: Scope,
        key: str,
        status: OperationStatus,
        *,
        now: datetime,
        resource: ResourceRef | None = None,
        result: Mapping[str, JsonValue] | None = None,
        fence: Lease | None = None,
    ) -> OperationRecord:
        async with self.data.lock:
            staged = self.data.snapshot()
            if fence is not None:
                leases.check(staged.leases.get(_thread_key(fence.thread)), fence, now)
            op_key = (*operations.operation_scope(scope), key)
            current = staged.operations.get(op_key)
            if current is None:
                raise KeyError(f"no operation {key!r}")
            advanced = operations.advance(
                current, status, now=now, resource=resource, result=result
            )
            staged.operations[op_key] = advanced
            self._commit("advance_operation", staged)
            return advanced

    # Leases

    async def acquire_lease(
        self, thread: ThreadRef, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _thread_key(thread)
            state, acquired = leases.acquire(
                staged.leases.get(key), thread, holder=holder, turn_id=turn_id, now=now, ttl=ttl
            )
            staged.leases[key] = state
            self._commit("acquire_lease", staged)
            return acquired

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _thread_key(lease.thread)
            state, renewed = leases.renew(staged.leases.get(key), lease, now=now, ttl=ttl)
            staged.leases[key] = state
            self._commit("renew_lease", staged)
            return renewed

    async def release_lease(self, lease: Lease) -> None:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = _thread_key(lease.thread)
            staged.leases[key] = leases.release(staged.leases.get(key), lease)
            self._commit("release_lease", staged)

    # Journal

    async def append_events(
        self,
        session: ResourceRef,
        entries: Sequence[JournalEntry],
        *,
        cursor: str,
        now: datetime,
        fence: Lease | None = None,
    ) -> JournalAppend:
        async with self.data.lock:
            staged = self.data.snapshot()
            if fence is not None:
                leases.check(staged.leases.get(_thread_key(fence.thread)), fence, now)
            existing = staged.events.get(session.id, ())
            planned, duplicates = journal.plan_append(
                session.id,
                staged.event_sources.get(session.id, frozenset()),
                len(existing),
                entries,
            )
            appended = tuple(event for _, event in planned)
            projection = journal.project_all(
                staged.projections.get(session.id) or journal.empty_projection(session, now),
                appended,
                cursor=cursor,
                now=now,
            )
            staged.events[session.id] = (*existing, *appended)
            staged.event_sources[session.id] = staged.event_sources.get(session.id, frozenset()) | {
                (entry.source_key, entry.revision) for entry, _ in planned
            }
            staged.projections[session.id] = projection
            self._commit("append_events", staged)
            return JournalAppend(appended=appended, duplicates=duplicates, projection=projection)

    async def read_events(
        self, session_id: str, *, after: int = -1, limit: int = 1000
    ) -> Sequence[Event]:
        events = self.data.events.get(session_id, ())
        return list(events[after + 1 : after + 1 + limit])

    async def projection(self, session_id: str) -> ProjectionSnapshot | None:
        return self.data.projections.get(session_id)

    # Usage

    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        async with self.data.lock:
            staged = self.data.snapshot()
            key = (binding_id, observation.id)
            applied = usage_ledger.apply_observation(staged.usage.get(key), binding_id, observation)
            if applied is None:
                return None
            staged.usage[key], row = applied
            staged.outbox[row.key] = row
            self._commit("record_usage", staged)
            return row

    async def pending_outbox(self, *, limit: int = 100) -> Sequence[OutboxRow]:
        return [row for row in self.data.outbox.values() if not row.applied][:limit]

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
