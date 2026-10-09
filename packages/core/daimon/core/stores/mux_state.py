"""The Postgres `mux.state.store.StateStore`.

Every decision comes from the pure rules in `mux.state`; this module only
reads rows, calls them and writes the result, one transaction per method.
Races are settled by the database: `ON CONFLICT` for first writers, `FOR
UPDATE` on the row being changed, `FOR SHARE` on the lease a fenced write
depends on (so a takeover waits for the write to commit) and a transaction
advisory lock per usage observation.

Lease expiry is judged against the database's wall clock
(`clock_timestamp()`), read after every lock the decision depends on is
held, so neither a caller with a stale clock nor a write that queued on a
lock until the lease expired can use an expired lease.
`trust_caller_clock=True` is for tests that need to move time.

The module functions take an `AsyncSession` so the host can run one inside
its own transaction: `mark_outbox_applied` belongs in the same transaction
as the `usage_events` and `tenant_ledger` rows it accounts for. Nothing in
Daimon calls this store yet.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from daimon.core._models import (
    AccountingOutbox,
    ChannelConfigRevision,
    Journal,
    JournalSession,
    MuxOperation,
    ProviderBinding,
    ProviderBindingSlot,
    ThreadLease,
    ThreadSession,
    UsageObservation,
)
from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot
from mux.contracts.resources import ProviderBinding as BindingContract
from mux.contracts.usage import UsageObservation as ObservationContract
from mux.errors import BindingConflict, ScopeViolation
from mux.state import journal, operations, usage_ledger
from mux.state import lease as leases
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease, LeaseState, Slot
from mux.state.operations import Begun, OperationRecord
from mux.state.store import (
    SESSION_REF,
    ConfigRevisionConflict,
    binding_slot,
    check_binding_successor,
)
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from pydantic import BaseModel, JsonValue
from sqlalchemy import ColumnElement, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _tenant(tenant_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(tenant_id)
    except ValueError:
        raise ScopeViolation(tenant_id, "not a Daimon tenant id") from None


def _slot_values(slot: Slot) -> dict[str, Any]:
    channel = slot.thread.channel
    return {
        "tenant_id": _tenant(channel.tenant_id),
        "platform": channel.platform,
        "channel_id": channel.channel_id,
        "thread_id": slot.thread.thread_id,
        "account_id": slot.account_id,
    }


def _where_slot(
    table: type[ProviderBindingSlot] | type[ThreadLease], slot: Slot
) -> list[ColumnElement[bool]]:
    values = _slot_values(slot)
    return [
        table.tenant_id == values["tenant_id"],
        table.platform == values["platform"],
        table.channel_id == values["channel_id"],
        table.thread_id == values["thread_id"],
        table.account_id.is_not_distinct_from(values["account_id"]),
    ]


def _json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


type Clock = Callable[[AsyncSession], Awaitable[datetime]]
"""Reads the time a lease decision is made at. Call it after taking the locks."""


async def database_clock(session: AsyncSession) -> datetime:
    """The database's wall clock at this moment, not at transaction start."""
    db_now = await session.scalar(select(func.clock_timestamp()))
    assert db_now is not None
    return db_now


def fixed_clock(now: datetime) -> Clock:
    """A clock stuck at `now`, for tests that move time by hand."""

    async def read(session: AsyncSession) -> datetime:
        return now

    return read


# Config revisions


async def put_config_revision(session: AsyncSession, revision: ConfigRevision) -> ConfigRevision:
    channel = revision.channel
    key = {
        "tenant_id": _tenant(channel.tenant_id),
        "platform": channel.platform,
        "channel_id": channel.channel_id,
        "local": revision.local,
    }
    inserted = await session.scalar(
        insert(ChannelConfigRevision)
        .values(**key, digest=revision.digest, revision=_json(revision))
        .on_conflict_do_nothing()
        .returning(ChannelConfigRevision.local)
    )
    if inserted is None:
        existing = await get_config_revision(session, channel, revision.local)
        if existing != revision:
            raise ConfigRevisionConflict(channel.channel_id, revision.local)
    return ConfigRevision.model_validate(_json(revision))


def _channel_where(channel: ChannelRef) -> list[ColumnElement[bool]]:
    return [
        ChannelConfigRevision.tenant_id == _tenant(channel.tenant_id),
        ChannelConfigRevision.platform == channel.platform,
        ChannelConfigRevision.channel_id == channel.channel_id,
    ]


async def get_config_revision(
    session: AsyncSession, channel: ChannelRef, local: int
) -> ConfigRevision | None:
    body = await session.scalar(
        select(ChannelConfigRevision.revision).where(
            *_channel_where(channel), ChannelConfigRevision.local == local
        )
    )
    return ConfigRevision.model_validate(body) if body is not None else None


async def latest_config_revision(
    session: AsyncSession, channel: ChannelRef
) -> ConfigRevision | None:
    body = await session.scalar(
        select(ChannelConfigRevision.revision)
        .where(*_channel_where(channel))
        .order_by(ChannelConfigRevision.local.desc())
        .limit(1)
    )
    return ConfigRevision.model_validate(body) if body is not None else None


# Bindings


async def _binding(session: AsyncSession, binding_id: str, generation: int) -> BindingContract:
    body = await session.scalar(
        select(ProviderBinding.binding).where(
            ProviderBinding.binding_id == binding_id, ProviderBinding.generation == generation
        )
    )
    assert body is not None, f"binding {binding_id} lost generation {generation}"
    return BindingContract.model_validate(body)


async def get_binding(session: AsyncSession, slot: Slot) -> BindingContract | None:
    row = (
        await session.execute(
            select(ProviderBindingSlot.binding_id, ProviderBindingSlot.generation).where(
                *_where_slot(ProviderBindingSlot, slot)
            )
        )
    ).one_or_none()
    return await _binding(session, row.binding_id, row.generation) if row else None


async def put_binding(
    session: AsyncSession, binding: BindingContract, *, expected_generation: int
) -> BindingContract:
    slot = binding_slot(binding)
    values = _slot_values(slot)
    current_slot = (
        await session.execute(
            select(ProviderBindingSlot.binding_id, ProviderBindingSlot.generation)
            .where(*_where_slot(ProviderBindingSlot, slot))
            .with_for_update()
        )
    ).one_or_none()
    current = (
        await _binding(session, current_slot.binding_id, current_slot.generation)
        if current_slot
        else None
    )
    check_binding_successor(current, binding, expected_generation=expected_generation)
    if current_slot is None:
        created = await session.scalar(
            insert(ProviderBindingSlot)
            .values(binding_id=binding.id, generation=binding.generation, **values)
            .on_conflict_do_nothing()
            .returning(ProviderBindingSlot.binding_id)
        )
        if created is None:
            winner = await get_binding(session, slot)
            if winner is not None:
                raise BindingConflict(expected_generation, winner.generation)
            raise ValueError(f"binding id {binding.id} already names another slot")
    else:
        await session.execute(
            update(ProviderBindingSlot)
            .where(ProviderBindingSlot.binding_id == binding.id)
            .values(generation=binding.generation)
        )
    await session.execute(
        insert(ProviderBinding).values(
            binding_id=binding.id,
            generation=binding.generation,
            tenant_id=values["tenant_id"],
            provider=binding.provider,
            profile=binding.profile,
            binding=_json(binding),
        )
    )
    session_id = binding.native_refs.get(SESSION_REF)
    if session_id is not None:
        await session.execute(
            insert(JournalSession)
            .values(session_id=session_id, tenant_id=values["tenant_id"], binding_id=binding.id)
            .on_conflict_do_nothing()
        )
        owner = await session.scalar(
            select(JournalSession.binding_id).where(JournalSession.session_id == session_id)
        )
        if owner != binding.id:
            raise ValueError(f"session {session_id} already belongs to another slot")
    return BindingContract.model_validate(_json(binding))


# Leases


async def _lease_state(session: AsyncSession, slot: Slot, *, lock: str) -> LeaseState | None:
    query = select(ThreadLease.last_fence, ThreadLease.active).where(
        *_where_slot(ThreadLease, slot)
    )
    query = query.with_for_update(read=lock == "share")
    row = (await session.execute(query)).one_or_none()
    if row is None:
        return None
    active = Lease.model_validate(row.active) if row.active is not None else None
    return LeaseState(slot=slot, last_fence=row.last_fence, active=active)


async def _locked_lease_state(session: AsyncSession, slot: Slot) -> LeaseState | None:
    await session.execute(
        insert(ThreadLease).values(**_slot_values(slot), last_fence=0).on_conflict_do_nothing()
    )
    return await _lease_state(session, slot, lock="update")


async def _save_lease(session: AsyncSession, state: LeaseState) -> None:
    await session.execute(
        update(ThreadLease)
        .where(*_where_slot(ThreadLease, state.slot))
        .values(
            last_fence=state.last_fence,
            active=_json(state.active) if state.active is not None else None,
        )
    )


async def _check_fence(
    session: AsyncSession, target: Slot | None, fence: Lease | None, clock: Clock
) -> None:
    state = await _lease_state(session, target, lock="share") if target else None
    leases.check_target(state, target, fence, await clock(session))


async def acquire_lease(
    session: AsyncSession,
    slot: Slot,
    *,
    holder: str,
    turn_id: str,
    clock: Clock,
    ttl: timedelta,
) -> Lease:
    state = await _locked_lease_state(session, slot)
    new_state, acquired = leases.acquire(
        state, slot, holder=holder, turn_id=turn_id, now=await clock(session), ttl=ttl
    )
    await _save_lease(session, new_state)
    return acquired


async def renew_lease(
    session: AsyncSession, lease: Lease, *, clock: Clock, ttl: timedelta
) -> Lease:
    state = await _lease_state(session, lease.slot, lock="update")
    new_state, renewed = leases.renew(state, lease, now=await clock(session), ttl=ttl)
    await _save_lease(session, new_state)
    return renewed


async def release_lease(session: AsyncSession, lease: Lease) -> None:
    state = await _lease_state(session, lease.slot, lock="update")
    await _save_lease(session, leases.release(state, lease))


# Operations


def _op_where(scope: Scope, key: str) -> list[ColumnElement[bool]]:
    tenant_id, account_id = operations.operation_scope(scope)
    return [
        MuxOperation.tenant_id == _tenant(tenant_id),
        MuxOperation.account_id == account_id,
        MuxOperation.key == key,
    ]


async def _operation(
    session: AsyncSession, scope: Scope, key: str, *, lock: bool
) -> OperationRecord | None:
    query = select(MuxOperation.record).where(*_op_where(scope, key))
    body = await session.scalar(query.with_for_update() if lock else query)
    return OperationRecord.model_validate(body) if body is not None else None


async def _save_operation(session: AsyncSession, scope: Scope, record: OperationRecord) -> None:
    await session.execute(
        update(MuxOperation)
        .where(*_op_where(scope, record.operation.key))
        .values(
            status=record.operation.status,
            record=_json(record),
            updated_at=record.operation.updated_at,
        )
    )


async def begin_operation(
    session: AsyncSession,
    scope: Scope,
    *,
    key: str,
    request_digest: str,
    operation_id: str,
    now: datetime,
    slot: Slot | None = None,
) -> Begun:
    def decide(existing: OperationRecord | None) -> Begun:
        return operations.begin(
            existing,
            scope,
            key=key,
            request_digest=request_digest,
            operation_id=operation_id,
            now=now,
            slot=slot,
        )

    begun = decide(await _operation(session, scope, key, lock=True))
    if not begun.fresh:
        return begun
    record = begun.record
    inserted = await session.scalar(
        insert(MuxOperation)
        .values(
            tenant_id=_tenant(record.tenant_id),
            account_id=record.account_id,
            key=key,
            principal_id=record.principal_id,
            status=record.operation.status,
            record=_json(record),
            updated_at=record.operation.updated_at,
        )
        .on_conflict_do_nothing()
        .returning(MuxOperation.key)
    )
    if inserted is None:
        return decide(await _operation(session, scope, key, lock=True))
    return Begun.model_validate(_json(begun))


async def get_operation(session: AsyncSession, scope: Scope, key: str) -> OperationRecord | None:
    record = await _operation(session, scope, key, lock=False)
    if record is not None:
        operations.check_owner(record, scope)
    return record


async def _owned(session: AsyncSession, scope: Scope, key: str) -> OperationRecord:
    record = await _operation(session, scope, key, lock=True)
    if record is None:
        raise KeyError(f"no operation {key!r}")
    operations.check_owner(record, scope)
    return record


async def claim_send(
    session: AsyncSession,
    scope: Scope,
    key: str,
    *,
    now: datetime,
    clock: Clock,
    fence: Lease | None,
) -> OperationRecord:
    record = await _owned(session, scope, key)
    await _check_fence(session, record.slot, fence, clock)
    claimed = operations.claim(record, now=now)
    await _save_operation(session, scope, claimed)
    return claimed


async def advance_operation(
    session: AsyncSession,
    scope: Scope,
    key: str,
    status: OperationStatus,
    *,
    now: datetime,
    clock: Clock,
    fence: Lease | None,
    resource: ResourceRef | None = None,
    result: Mapping[str, JsonValue] | None = None,
) -> OperationRecord:
    record = await _owned(session, scope, key)
    await _check_fence(session, record.slot, fence, clock)
    advanced = operations.advance(record, status, now=now, resource=resource, result=result)
    await _save_operation(session, scope, advanced)
    return OperationRecord.model_validate(_json(advanced))


# Journal


async def _session_owner(session: AsyncSession, session_id: str) -> tuple[str, Slot] | None:
    row = (
        await session.execute(
            select(JournalSession.binding_id).where(JournalSession.session_id == session_id)
        )
    ).one_or_none()
    if row is None:
        return None
    slot_row = (
        await session.execute(
            select(ProviderBindingSlot.generation).where(
                ProviderBindingSlot.binding_id == row.binding_id
            )
        )
    ).one()
    binding = await _binding(session, row.binding_id, slot_row.generation)
    return row.binding_id, binding_slot(binding)


async def append_events(
    session: AsyncSession,
    target: ResourceRef,
    entries: Sequence[JournalEntry],
    *,
    fence: Lease,
    cursor: str,
    now: datetime,
    clock: Clock,
) -> JournalAppend:
    head = (
        await session.execute(
            select(
                JournalSession.next_sequence, JournalSession.projection, JournalSession.tenant_id
            )
            .where(JournalSession.session_id == target.id)
            .with_for_update()
        )
    ).one_or_none()
    owner = await _session_owner(session, target.id) if head else None
    if head is None or owner is None:
        raise ScopeViolation(target.id, "no binding names this session")
    await _check_fence(session, owner[1], fence, clock)
    incoming = [JournalEntry.model_validate(_json(entry)) for entry in entries]
    keys = {entry.source_key for entry in incoming}
    seen: set[tuple[str, int, bool]] = set()
    if keys:
        rows = await session.execute(
            select(Journal.source_key, Journal.revision, Journal.preview).where(
                Journal.session_id == target.id, Journal.source_key.in_(keys)
            )
        )
        seen = {(row.source_key, row.revision, row.preview) for row in rows}
    planned, duplicates = journal.plan_append(target.id, seen, head.next_sequence, incoming)
    appended = tuple(event for _, event in planned)
    prior = (
        ProjectionSnapshot.model_validate(head.projection)
        if head.projection is not None
        else journal.empty_projection(target, now)
    )
    projection = journal.project_all(prior, appended, cursor=cursor, now=now)
    if planned:
        await session.execute(
            insert(Journal),
            [
                {
                    "session_id": target.id,
                    "sequence": event.sequence,
                    "tenant_id": head.tenant_id,
                    "source_key": entry.source_key,
                    "revision": entry.revision,
                    "preview": entry.identity[2],
                    "event": _json(event),
                }
                for entry, event in planned
            ],
        )
    await session.execute(
        update(JournalSession)
        .where(JournalSession.session_id == target.id)
        .values(next_sequence=head.next_sequence + len(planned), projection=_json(projection))
    )
    return JournalAppend.model_validate(
        _json(JournalAppend(appended=appended, duplicates=duplicates, projection=projection))
    )


async def read_events(
    session: AsyncSession, session_id: str, *, after: int = -1, limit: int = 1000
) -> list[Event]:
    bodies = await session.scalars(
        select(Journal.event)
        .where(Journal.session_id == session_id, Journal.sequence > after)
        .order_by(Journal.sequence)
        .limit(limit)
    )
    return [Event.model_validate(body) for body in bodies]


async def projection(session: AsyncSession, session_id: str) -> ProjectionSnapshot | None:
    body = await session.scalar(
        select(JournalSession.projection).where(JournalSession.session_id == session_id)
    )
    return ProjectionSnapshot.model_validate(body) if body is not None else None


# Usage and the accounting outbox


async def record_usage(
    session: AsyncSession, binding_id: str, observation: ObservationContract
) -> OutboxRow | None:
    owner = await _session_owner(session, observation.session.id)
    if owner is None or owner[0] != binding_id:
        raise ScopeViolation(binding_id, "binding does not own the observed session")
    tenant_id = _tenant(owner[1].tenant_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mux.usage\x1f{binding_id}\x1f{observation.id}"},
    )
    prior_body = await session.scalar(
        select(UsageObservation.applied)
        .where(
            UsageObservation.binding_id == binding_id,
            UsageObservation.observation_id == observation.id,
        )
        .order_by(UsageObservation.revision.desc())
        .limit(1)
    )
    prior = AppliedUsage.model_validate(prior_body) if prior_body is not None else None
    outcome = usage_ledger.apply_observation(
        prior, binding_id, ObservationContract.model_validate(_json(observation))
    )
    if outcome is None:
        return None
    applied, row = outcome
    await session.execute(
        insert(UsageObservation).values(
            binding_id=binding_id,
            observation_id=observation.id,
            revision=applied.revision,
            tenant_id=tenant_id,
            applied=_json(applied),
        )
    )
    await session.execute(
        insert(AccountingOutbox).values(
            binding_id=binding_id,
            observation_id=observation.id,
            revision=row.revision,
            prior_applied_revision=row.prior_applied_revision,
            tenant_id=tenant_id,
            row=_json(row),
        )
    )
    return OutboxRow.model_validate(_json(row))


async def pending_outbox(session: AsyncSession, *, limit: int = 100) -> list[OutboxRow]:
    bodies = await session.scalars(
        select(AccountingOutbox.row)
        .where(AccountingOutbox.applied_at.is_(None))
        .order_by(
            AccountingOutbox.created_at,
            AccountingOutbox.binding_id,
            AccountingOutbox.observation_id,
            AccountingOutbox.revision,
        )
        .limit(limit)
    )
    return [OutboxRow.model_validate(body) for body in bodies]


async def mark_outbox_applied(session: AsyncSession, row: OutboxRow) -> bool:
    """True the first time. Run it in the transaction that writes the ledger rows."""
    where = (
        AccountingOutbox.binding_id == row.binding_id,
        AccountingOutbox.observation_id == row.observation_id,
        AccountingOutbox.revision == row.revision,
    )
    marked = await session.scalar(
        update(AccountingOutbox)
        .where(*where, AccountingOutbox.applied_at.is_(None))
        .values(applied_at=func.now())
        .returning(AccountingOutbox.revision)
    )
    if marked is not None:
        return True
    if await session.scalar(select(AccountingOutbox.revision).where(*where)) is None:
        raise KeyError(f"no outbox row {row.key}")
    return False


# Legacy thread_sessions links


async def link_legacy_thread_sessions(session: AsyncSession, *, batch: int = 2000) -> int:
    """Fill `binding_id`/`binding_generation` on up to `batch` backfilled rows.

    Migration 0074 leaves them NULL so it never holds a long lock on
    `thread_sessions`. Run this in its own short transaction, repeatedly,
    until it returns 0; rows already linked are skipped, so it is safe to
    stop and rerun at any time.
    """
    pending = (
        select(ThreadSession.id)
        .join(ProviderBinding, ProviderBinding.legacy_row_id == ThreadSession.id)
        .where(ThreadSession.binding_id.is_(None))
        .limit(batch)
        .with_for_update(of=ThreadSession, skip_locked=True)
        .scalar_subquery()
    )
    linked = await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id.in_(pending), ProviderBinding.legacy_row_id == ThreadSession.id)
        .values(
            binding_id=ProviderBinding.binding_id, binding_generation=ProviderBinding.generation
        )
        .returning(ThreadSession.id)
    )
    return len(linked.all())


class PostgresStateStore:
    """The `StateStore` protocol over the functions above, one transaction per call."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        trust_caller_clock: bool = False,
    ) -> None:
        self._factory = session_factory
        self._trust_caller_clock = trust_caller_clock

    def _clock(self, now: datetime) -> Clock:
        return fixed_clock(now) if self._trust_caller_clock else database_clock

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session, session.begin():
            yield session

    async def put_config_revision(self, revision: ConfigRevision) -> ConfigRevision:
        async with self._tx() as session:
            return await put_config_revision(session, revision)

    async def get_config_revision(self, channel: ChannelRef, local: int) -> ConfigRevision | None:
        async with self._tx() as session:
            return await get_config_revision(session, channel, local)

    async def latest_config_revision(self, channel: ChannelRef) -> ConfigRevision | None:
        async with self._tx() as session:
            return await latest_config_revision(session, channel)

    async def get_binding(self, slot: Slot) -> BindingContract | None:
        async with self._tx() as session:
            return await get_binding(session, slot)

    async def put_binding(
        self, binding: BindingContract, *, expected_generation: int
    ) -> BindingContract:
        async with self._tx() as session:
            return await put_binding(session, binding, expected_generation=expected_generation)

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
        async with self._tx() as session:
            return await begin_operation(
                session,
                scope,
                key=key,
                request_digest=request_digest,
                operation_id=operation_id,
                now=now,
                slot=slot,
            )

    async def get_operation(self, scope: Scope, key: str) -> OperationRecord | None:
        async with self._tx() as session:
            return await get_operation(session, scope, key)

    async def claim_send(
        self, scope: Scope, key: str, *, now: datetime, fence: Lease | None
    ) -> OperationRecord:
        async with self._tx() as session:
            return await claim_send(
                session, scope, key, now=now, clock=self._clock(now), fence=fence
            )

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
        async with self._tx() as session:
            return await advance_operation(
                session,
                scope,
                key,
                status,
                now=now,
                clock=self._clock(now),
                fence=fence,
                resource=resource,
                result=result,
            )

    async def acquire_lease(
        self, slot: Slot, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease:
        async with self._tx() as session:
            return await acquire_lease(
                session, slot, holder=holder, turn_id=turn_id, clock=self._clock(now), ttl=ttl
            )

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease:
        async with self._tx() as session:
            return await renew_lease(session, lease, clock=self._clock(now), ttl=ttl)

    async def release_lease(self, lease: Lease) -> None:
        async with self._tx() as session:
            await release_lease(session, lease)

    async def append_events(
        self,
        session: ResourceRef,
        entries: Sequence[JournalEntry],
        *,
        fence: Lease,
        cursor: str,
        now: datetime,
    ) -> JournalAppend:
        async with self._tx() as db:
            return await append_events(
                db, session, entries, fence=fence, cursor=cursor, now=now, clock=self._clock(now)
            )

    async def read_events(
        self, session_id: str, *, after: int = -1, limit: int = 1000
    ) -> Sequence[Event]:
        async with self._tx() as session:
            return await read_events(session, session_id, after=after, limit=limit)

    async def projection(self, session_id: str) -> ProjectionSnapshot | None:
        async with self._tx() as session:
            return await projection(session, session_id)

    async def record_usage(
        self, binding_id: str, observation: ObservationContract
    ) -> OutboxRow | None:
        async with self._tx() as session:
            return await record_usage(session, binding_id, observation)

    async def pending_outbox(self, *, limit: int = 100) -> Sequence[OutboxRow]:
        async with self._tx() as session:
            return await pending_outbox(session, limit=limit)

    async def mark_outbox_applied(self, row: OutboxRow) -> bool:
        async with self._tx() as session:
            return await mark_outbox_applied(session, row)
