"""The `StateStore` protocol, and the binding rules every store enforces.

Each method is one atomic transaction: it either commits everything it
decides or nothing. The decisions themselves come from the pure functions
in `operations`, `lease`, `journal` and `usage_ledger`; a store only reads,
calls them and writes. Time is passed in, never read from a clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import JsonValue

from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, MuxError
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease
from mux.state.operations import Begun, OperationRecord
from mux.state.usage_ledger import OutboxRow


class ConfigRevisionConflict(MuxError):
    """A channel's config revision number was reused for different content."""

    def __init__(self, channel_id: str, local: int) -> None:
        super().__init__(f"config revision {local} of channel {channel_id} already differs")
        self.channel_id = channel_id
        self.local = local


def check_binding_successor(
    current: ProviderBinding | None, binding: ProviderBinding, *, expected_generation: int
) -> None:
    """Compare-and-swap rules for writing `binding` over `current`.

    The thread's generation must still be `expected_generation` (0 for an
    unbound thread), the new binding must be the next generation, and a
    rebind keeps the binding's `id`.
    """
    actual = current.generation if current else 0
    if actual != expected_generation:
        raise BindingConflict(expected_generation, actual)
    if binding.generation != expected_generation + 1:
        raise ValueError(
            f"binding generation must be {expected_generation + 1}, got {binding.generation}"
        )
    if current is not None and current.id != binding.id:
        raise ValueError(f"a rebind keeps binding id {current.id}, got {binding.id}")


class StateStore(Protocol):
    # Channel config revisions: immutable, unique on (channel, local).
    async def put_config_revision(self, revision: ConfigRevision) -> ConfigRevision:
        """Store a revision. The same content again is a no-op; different
        content under a used number raises `ConfigRevisionConflict`."""
        ...

    async def get_config_revision(
        self, channel: ChannelRef, local: int
    ) -> ConfigRevision | None: ...
    async def latest_config_revision(self, channel: ChannelRef) -> ConfigRevision | None: ...

    # Thread bindings: unique on (thread, generation), compare-and-swap on generation.
    async def get_binding(self, thread: ThreadRef) -> ProviderBinding | None:
        """The thread's current (highest-generation) binding."""
        ...

    async def put_binding(
        self, binding: ProviderBinding, *, expected_generation: int
    ) -> ProviderBinding:
        """Write the next generation per `check_binding_successor`; the loser
        of a race gets `BindingConflict`."""
        ...

    # Operations: unique on (tenant, account, key).
    async def begin_operation(
        self, scope: Scope, *, key: str, request_digest: str, operation_id: str, now: datetime
    ) -> Begun: ...

    async def get_operation(self, scope: Scope, key: str) -> OperationRecord | None: ...

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
        """Move an operation along `operations.TRANSITIONS`. With `fence`,
        refuses (`StaleFence`) unless that lease is still active."""
        ...

    # Thread leases.
    async def acquire_lease(
        self, thread: ThreadRef, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease: ...

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease: ...
    async def release_lease(self, lease: Lease) -> None: ...

    # Journal: unique on (session, sequence) and (session, source_key, revision).
    async def append_events(
        self,
        session: ResourceRef,
        entries: Sequence[JournalEntry],
        *,
        cursor: str,
        now: datetime,
        fence: Lease | None = None,
    ) -> JournalAppend:
        """Append the new entries, fold them into the projection and move the
        cursor, all in one commit."""
        ...

    async def read_events(
        self, session_id: str, *, after: int = -1, limit: int = 1000
    ) -> Sequence[Event]: ...

    async def projection(self, session_id: str) -> ProjectionSnapshot | None: ...

    # Usage revisions and the accounting outbox.
    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        """Apply a revision and enqueue its outbox row; None if stale."""
        ...

    async def pending_outbox(self, *, limit: int = 100) -> Sequence[OutboxRow]: ...

    async def mark_outbox_applied(self, row: OutboxRow) -> bool:
        """True the first time; False if the row was already applied."""
        ...


async def bind_new_thread(store: StateStore, binding: ProviderBinding) -> ProviderBinding:
    """Bind an unbound thread, or return the binding that won a race to it."""
    try:
        return await store.put_binding(binding, expected_generation=0)
    except BindingConflict:
        winner = await store.get_binding(binding.thread)
        if winner is None:
            raise
        return winner
