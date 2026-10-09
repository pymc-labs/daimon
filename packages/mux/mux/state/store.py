"""The `StateStore` protocol, and the binding rules every store enforces.

Each method is one atomic transaction: it either commits everything it
decides or nothing. The decisions come from the pure functions in
`operations`, `lease`, `journal` and `usage_ledger`; a store only reads,
calls them and writes. Records and payloads a store returns are its own
copies: changing one never changes what was committed.

Fencing. Writes that a superseded worker could get wrong take the slot's
lease and are refused without the active one: `claim_send` and
`advance_operation` on an operation begun with a slot, and every
`append_events`. A session's journal belongs to the slot of the binding
whose `native_refs["session"]` names it, registered by `put_binding`; an
append never establishes ownership, and a session no binding names takes
no appends. The others are safe without a lease: `begin_operation`
only records intent and is idempotent; `put_binding` is its own
compare-and-swap on generation; `record_usage` is ordered by revision, so a
stale writer can only report true usage or be ignored; `mark_outbox_applied`
flips once.

Time. `now` is passed in so the rules stay pure. A database store checks
lease expiry against its own transaction clock, not the argument: a caller
with a slow or stale clock must not keep an expired lease alive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import JsonValue

from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, MuxError
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease, Slot
from mux.state.operations import Begun, OperationRecord
from mux.state.usage_ledger import OutboxRow

SESSION_REF = "session"
"""The `ProviderBinding.native_refs` key naming the session a journal is for."""


class ConfigRevisionConflict(MuxError):
    """A channel's config revision number was reused for different content."""

    def __init__(self, channel_id: str, local: int) -> None:
        super().__init__(f"config revision {local} of channel {channel_id} already differs")
        self.channel_id = channel_id
        self.local = local


def binding_slot(binding: ProviderBinding) -> Slot:
    """A binding's slot: private to `legacy_account_id` when set, else shared."""
    return Slot(thread=binding.thread, account_id=binding.legacy_account_id)


def check_binding_successor(
    current: ProviderBinding | None, binding: ProviderBinding, *, expected_generation: int
) -> None:
    """Compare-and-swap rules for writing `binding` over `current` in its slot.

    The slot's generation must still be `expected_generation` (0 for an
    unbound slot), the new binding must be the next generation, and a
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

    # Bindings: unique on (slot, generation); compare-and-swap on generation.
    # A binding id names one slot only.
    async def get_binding(self, slot: Slot) -> ProviderBinding | None:
        """The slot's current (highest-generation) binding."""
        ...

    async def put_binding(
        self, binding: ProviderBinding, *, expected_generation: int
    ) -> ProviderBinding:
        """Write the next generation in `binding_slot(binding)` per
        `check_binding_successor`; the loser of a race gets `BindingConflict`.

        Registers the binding's session as owned by its slot. A rebind keeps
        the old sessions' ownership; a session already owned by another slot
        is refused.
        """
        ...

    # Operations: found by (tenant, account, key), owned by one principal.
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
        """Persist intent. `slot` binds the operation to a slot's lease."""
        ...

    async def get_operation(self, scope: Scope, key: str) -> OperationRecord | None: ...

    async def claim_send(
        self, scope: Scope, key: str, *, now: datetime, fence: Lease | None
    ) -> OperationRecord:
        """`pending → sent` as a compare-and-swap: `SendClaimed` for every
        caller but one. Only the winner issues the request."""
        ...

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
        """Record an observed status along `operations.TRANSITIONS`."""
        ...

    # Slot leases.
    async def acquire_lease(
        self, slot: Slot, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease: ...

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease: ...
    async def release_lease(self, lease: Lease) -> None: ...

    # Journal: unique on (session, sequence) and (session, source_key, revision,
    # preview). A session belongs to the slot of the binding that names it.
    async def append_events(
        self,
        session: ResourceRef,
        entries: Sequence[JournalEntry],
        *,
        fence: Lease,
        cursor: str,
        now: datetime,
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


async def bind_new_slot(store: StateStore, binding: ProviderBinding) -> ProviderBinding:
    """Bind an unbound slot, or return the binding that won a race to it."""
    try:
        return await store.put_binding(binding, expected_generation=0)
    except BindingConflict:
        winner = await store.get_binding(binding_slot(binding))
        if winner is None:
            raise
        return winner
