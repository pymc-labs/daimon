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
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from daimon.core._models import (
    AccountingOutbox,
    ChannelConfigRevision,
    FleetRun,
    Journal,
    JournalSession,
    MuxOperation,
    ProviderBinding,
    ProviderBindingSlot,
    ThreadLease,
    ThreadSession,
    TurnCardIntent,
    UsageObservation,
)
from daimon.core.stores import agent_backends, check_persisted_read, read_persisted_columns
from daimon.core.stores.domain import AgentBackendRevision
from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, Scope
from mux.contracts.receipts import OperationStatus
from mux.contracts.resources import ProjectionSnapshot
from mux.contracts.resources import ProviderBinding as BindingContract
from mux.contracts.usage import UsageObservation as ObservationContract
from mux.errors import BindingConflict, OperationConflict, ScopeViolation
from mux.state import journal, operations, usage_ledger
from mux.state import lease as leases
from mux.state.journal import JournalAppend, JournalEntry
from mux.state.lease import Lease, LeaseState, Slot
from mux.state.operations import Begun, OperationRecord
from mux.state.store import (
    SESSION_REF,
    ConfigRevisionConflict,
    adopt_released,
    binding_slot,
    check_binding_successor,
    is_released,
)
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from sqlalchemy import ColumnElement, String, cast, func, select, text, tuple_, update
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


type BeforeClaim = Callable[[AsyncSession, Scope, str, Lease | None], Awaitable[bool]]
"""Runs in a send claim's transaction just before the claim. True claims (and a
lost claim rolls back what it wrote); False commits what it wrote and refuses."""

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


def _agent_backend_reference(
    channel: ChannelRef, origin: str, revision: AgentBackendRevision | None
) -> list[JsonValue] | None:
    if revision is None:
        return None
    if revision.tenant_id != _tenant(channel.tenant_id) or origin != "agent":
        raise ScopeViolation(channel.channel_id, "foreign agent backend association")
    return [str(revision.agent_id), revision.local]


async def put_config_revision(
    session: AsyncSession,
    revision: ConfigRevision,
    *,
    origin: str = "channel",
    agent_revision: AgentBackendRevision | None = None,
) -> ConfigRevision:
    channel = revision.channel
    key = {
        "tenant_id": _tenant(channel.tenant_id),
        "platform": channel.platform,
        "channel_id": channel.channel_id,
        "local": revision.local,
    }
    reference = _agent_backend_reference(channel, origin, agent_revision)
    inserted = await session.scalar(
        insert(ChannelConfigRevision)
        .values(
            **key,
            digest=revision.digest,
            revision=_json(revision),
            origin=origin,
            agent_backend_ref=reference,
        )
        .on_conflict_do_nothing()
        .returning(ChannelConfigRevision.local)
    )
    if inserted is None:
        existing = await get_config_revision(session, channel, revision.local)
        retained = await session.scalar(
            select(ChannelConfigRevision.agent_backend_ref).where(
                *_channel_where(channel), ChannelConfigRevision.local == revision.local
            )
        )
        if existing != revision or retained != reference:
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
    session: AsyncSession, channel: ChannelRef, *, overrides_only: bool = False
) -> ConfigRevision | None:
    selection = _channel_where(channel)
    if overrides_only:
        selection.extend(
            (
                ChannelConfigRevision.origin.in_(("channel", "channel_clear")),
                ChannelConfigRevision.local > 0,
            )
        )
    body = await session.scalar(
        select(ChannelConfigRevision.revision)
        .where(*selection)
        .order_by(ChannelConfigRevision.local.desc())
        .limit(1)
    )
    return ConfigRevision.model_validate(body) if body is not None else None


async def latest_config_revision_and_sharing(
    session: AsyncSession, channel: ChannelRef, *, overrides_only: bool = False
) -> tuple[ConfigRevision | None, bool]:
    """The channel's newest revision, and whether any of its revisions was shared.

    One statement, so reading the sharing history costs admission nothing more
    than the revision it already reads.
    """
    ever_shared = (
        select(ChannelConfigRevision.local)
        .where(
            *_channel_where(channel),
            ChannelConfigRevision.revision["thread_mode"].astext == "shared",
        )
        .exists()
    )
    selection = _channel_where(channel)
    if overrides_only:
        selection.extend(
            (
                ChannelConfigRevision.origin.in_(("channel", "channel_clear")),
                ChannelConfigRevision.local > 0,
            )
        )
    latest = (
        select(ChannelConfigRevision.revision)
        .where(*selection)
        .order_by(ChannelConfigRevision.local.desc())
        .limit(1)
        .scalar_subquery()
    )
    # One row even with no override: agent-origin revisions may have shared threads.
    revision, shared = (await session.execute(select(latest, ever_shared))).one()
    if revision is None:
        return None, bool(shared)
    return ConfigRevision.model_validate(revision), bool(shared)


async def thread_has_caller_sessions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    except_account: uuid.UUID,
) -> bool:
    """Whether anyone but `except_account` has a session row in the thread, of any status."""
    found = await session.scalar(
        select(ThreadSession.id)
        .where(
            ThreadSession.tenant_id == tenant_id,
            ThreadSession.platform == platform,
            ThreadSession.thread_id == thread_id,
            ThreadSession.account_id.is_distinct_from(except_account),
        )
        .limit(1)
    )
    return found is not None


# Bindings


async def _binding(
    session: AsyncSession, binding_id: str, generation: int, *, read_persisted: bool = False
) -> BindingContract:
    statement = select(ProviderBinding.binding).where(
        ProviderBinding.binding_id == binding_id, ProviderBinding.generation == generation
    )
    body = (
        (await read_persisted_columns(session, statement)).scalar_one_or_none()
        if read_persisted
        else await session.scalar(statement)
    )
    assert body is not None, f"binding {binding_id} lost generation {generation}"
    return BindingContract.model_validate(body)


async def get_binding(
    session: AsyncSession,
    slot: Slot,
    *,
    read_persisted: bool = False,
    include_released: bool = False,
) -> BindingContract | None:
    """The slot's binding; None for a released slot (`is_released`) unless asked."""
    statement = select(ProviderBindingSlot.binding_id, ProviderBindingSlot.generation).where(
        *_where_slot(ProviderBindingSlot, slot)
    )
    row = (
        await read_persisted_columns(session, statement)
        if read_persisted
        else await session.execute(statement)
    ).one_or_none()
    if row is None:
        return None
    binding = await _binding(session, row.binding_id, row.generation, read_persisted=read_persisted)
    return None if is_released(binding) and not include_released else binding


type SlotKey = tuple[uuid.UUID, str, str, str, str | None]


def slot_key(slot: Slot) -> SlotKey:
    """The columns `get_binding` matches a slot on, as one comparable key."""
    values = _slot_values(slot)
    return (
        values["tenant_id"],
        values["platform"],
        values["channel_id"],
        values["thread_id"],
        values["account_id"],
    )


@dataclass(frozen=True)
class SlotBindings:
    """Binding rows of many slots from one batched read in the caller's transaction.

    `slots` holds every slot key that was read: its (binding id, generation), or None
    when the slot has no row. `bodies` holds every (binding id, generation) that was read.
    """

    slots: Mapping[SlotKey, tuple[str, int] | None]
    bodies: Mapping[tuple[str, int], dict[str, Any] | None]


async def read_slot_bindings(session: AsyncSession, slots: Iterable[Slot]) -> SlotBindings:
    """`get_binding`'s two reads for every slot at once: two statements in total.

    Reads only; refuses nothing. `cached_binding` answers `get_binding` from the result,
    with the same refusals at the point the per-slot read would have refused.
    """
    keys = {slot_key(slot) for slot in slots}
    if not keys:
        return SlotBindings({}, {})
    with session.no_autoflush:
        found = {
            (row.tenant_id, row.platform, row.channel_id, row.thread_id, row.account_id): (
                row.binding_id,
                row.generation,
            )
            for row in await session.execute(
                select(
                    ProviderBindingSlot.tenant_id,
                    ProviderBindingSlot.platform,
                    ProviderBindingSlot.channel_id,
                    ProviderBindingSlot.thread_id,
                    ProviderBindingSlot.account_id,
                    ProviderBindingSlot.binding_id,
                    ProviderBindingSlot.generation,
                ).where(
                    tuple_(
                        ProviderBindingSlot.tenant_id,
                        ProviderBindingSlot.platform,
                        ProviderBindingSlot.channel_id,
                        ProviderBindingSlot.thread_id,
                    ).in_({key[:4] for key in keys})
                )
            )
        }
        located = {key: found.get(key) for key in keys}
        pairs = {pair for pair in located.values() if pair is not None}
        bodies: dict[tuple[str, int], dict[str, Any] | None] = dict.fromkeys(pairs)
        if pairs:
            for row in await session.execute(
                select(
                    ProviderBinding.binding_id, ProviderBinding.generation, ProviderBinding.binding
                ).where(tuple_(ProviderBinding.binding_id, ProviderBinding.generation).in_(pairs))
            ):
                bodies[(row.binding_id, row.generation)] = row.binding
    return SlotBindings(located, bodies)


async def cached_binding(
    session: AsyncSession,
    cache: SlotBindings,
    slot: Slot,
    *,
    read_persisted: bool = False,
    include_released: bool = False,
) -> BindingContract | None:
    """`get_binding` answered from `read_slot_bindings`; a slot it did not read is read now."""
    key = slot_key(slot)
    if key not in cache.slots:
        return await get_binding(
            session, slot, read_persisted=read_persisted, include_released=include_released
        )
    if read_persisted:
        check_persisted_read(session, select(ProviderBindingSlot.binding_id))
    pair = cache.slots[key]
    if pair is None:
        return None
    if read_persisted:
        check_persisted_read(session, select(ProviderBinding.binding))
    binding_id, generation = pair
    body = cache.bodies[pair]
    assert body is not None, f"binding {binding_id} lost generation {generation}"
    binding = BindingContract.model_validate(body)
    return None if is_released(binding) and not include_released else binding


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
    binding, expected_generation = adopt_released(
        current, binding, expected_generation=expected_generation
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


async def require_lease(session: AsyncSession, lease: Lease) -> None:
    """Fence host authorization writes in the same transaction as their lease check.

    The shared row lock prevents a takeover until the authorized write commits.
    """
    await _check_fence(session, lease.slot, lease, database_clock)


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


async def active_lease(session: AsyncSession, slot: Slot) -> Lease | None:
    """The slot's active lease as stored, expired or not; None if it has none.

    A plain read, without a row lock, so a sweep never blocks lease renewals.
    """
    active = (
        await session.execute(select(ThreadLease.active).where(*_where_slot(ThreadLease, slot)))
    ).scalar_one_or_none()
    return Lease.model_validate(active) if active is not None else None


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


async def get_fleet_root_operation(
    session: AsyncSession,
    scope: Scope,
    *,
    intent_id: uuid.UUID | None = None,
    origin_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    request_digest: str | None = None,
    slot: Slot | None = None,
    check_intent_owner: bool = False,
    root_controls: Mapping[str, JsonValue] | None = None,
    root_finance: Mapping[str, JsonValue] | None = None,
    for_update: bool = False,
) -> OperationRecord | None:
    """Read or durably reserve the exact root association before run creation."""
    if check_intent_owner and (
        intent_id is None or (origin_id, run_id, request_digest) != (None, None, None)
    ):
        raise ValueError("intent-only root read required")
    if request_digest is None and (root_controls is not None or root_finance is not None):
        raise ValueError("control initialization requires a reservation")
    if for_update and (
        origin_id is None
        or intent_id is not None
        or run_id is not None
        or request_digest is not None
    ):
        raise ValueError("locked read requires the original origin selector")
    query = select(MuxOperation).where(
        MuxOperation.tenant_id == _tenant(scope.tenant_id),
        MuxOperation.key.startswith("fleet_root:"),
        MuxOperation.record["result"]["intent_id"].astext == str(intent_id),
    )
    if request_digest is not None:
        if root_finance is not None and root_controls is None:
            raise ValueError("root finance requires immutable controls")
        if intent_id is None or origin_id is None or slot is None or run_id is not None:
            raise ValueError("complete root reservation required")
        intent = await session.scalar(
            select(TurnCardIntent.id)
            .where(
                TurnCardIntent.id == intent_id,
                TurnCardIntent.tenant_id == _tenant(scope.tenant_id),
                TurnCardIntent.status.in_(("prepared", "posted")),
            )
            .with_for_update()
        )
        if intent is None:
            raise ScopeViolation(str(intent_id), "root intent is unavailable")
        key = f"fleet_root:{origin_id}:root:0"
        previous = (await read_persisted_columns(session, query)).one_or_none()
        if previous is not None and (previous.account_id, previous.key) != (scope.account_id, key):
            raise OperationConflict(key)
        begun = await begin_operation(
            session,
            scope,
            key=key,
            request_digest=request_digest,
            operation_id=key,
            slot=slot,
            now=await database_clock(session),
        )
        record = begun.record
        if (
            not begun.fresh
            and root_controls is not None
            and record.result.get("root_controls") != dict(root_controls)
        ):
            raise OperationConflict(key)
        if begun.fresh:
            result: dict[str, JsonValue] = {
                "intent_id": str(intent_id),
                "origin_id": str(origin_id),
            }
            if root_controls is not None:
                result.update(
                    root_controls=dict(root_controls), root_finance=dict(root_finance or {})
                )
            record = record.model_copy(update={"result": result})
            await _save_operation(session, scope, record)
        return record
    if run_id is not None:
        if intent_id is not None or origin_id is not None:
            raise ValueError("exactly one root selector required")
        run = await session.scalar(
            select(FleetRun).where(
                FleetRun.id == run_id,
                FleetRun.tenant_id == _tenant(scope.tenant_id),
                FleetRun.orchestrator_account_id == _tenant(scope.account_id),
            )
        )
        if run is None:
            return None
        return await get_operation(session, scope, run.operation_key + ":root:0")
    if (intent_id is None) == (origin_id is None):
        raise ValueError("exactly one root selector required")
    if origin_id is not None:
        key = f"fleet_root:{origin_id}:root:0"
        original = select(MuxOperation.record).where(*_op_where(scope, key))
        body = (
            await read_persisted_columns(
                session, original.with_for_update() if for_update else original
            )
        ).scalar_one_or_none()
        record = OperationRecord.model_validate(body) if body is not None else None
        if record is not None:
            operations.check_owner(record, scope)
        return record
    if not check_intent_owner:
        query = query.where(MuxOperation.account_id == scope.account_id)
    body = (await read_persisted_columns(session, query)).one_or_none()
    if body is None:
        return None
    record = OperationRecord.model_validate(body.record)
    operations.check_owner(record, scope)
    return record


async def update_fleet_root_finance(
    session: AsyncSession,
    scope: Scope,
    *,
    origin_id: uuid.UUID,
    root_finance: Mapping[str, JsonValue],
) -> OperationRecord:
    """Update only mutable finance; caller holds fleet.lock_budget for the tenant first."""
    record = await _owned(session, scope, f"fleet_root:{origin_id}:root:0")
    if "root_controls" not in record.result:
        raise OperationConflict(record.operation.key)
    updated = record.model_copy(
        update={"result": {**record.result, "root_finance": dict(root_finance)}}
    )
    await _save_operation(session, scope, updated)
    return updated


async def list_fleet_root_finances(
    session: AsyncSession, *, tenant_id: uuid.UUID, locking: bool = True
) -> tuple[OperationRecord, ...]:
    """Retained financial roots, including unknown pre-allocation holds across actors.

    Caller takes the tenant financial fence first. Column reads avoid stale ORM JSON.
    Without `locking` this reads the caller's read-only snapshot and locks nothing.
    """
    statement = (
        select(MuxOperation.record)
        .where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.key.startswith("fleet_root:", autoescape=True),
            MuxOperation.record["result"]["root_finance"].is_not(None),
        )
        .order_by(MuxOperation.key)
    )
    if locking:
        statement = statement.with_for_update()
    bodies = await read_persisted_columns(session, statement, reject_new=True)
    records = tuple(OperationRecord.model_validate(row.record) for row in bodies)
    if any(record.tenant_id != str(tenant_id) for record in records):
        raise ScopeViolation("root-budget", "financial root belongs to another tenant")
    return records


async def fleet_root_context_deadline(
    session: AsyncSession,
    *,
    origin_id: uuid.UUID | None = None,
    intent_id: uuid.UUID | None = None,
    now: datetime,
) -> datetime | None:
    """Bound lifecycle retention by this exact processed root's live run."""
    if (origin_id is None) == (intent_id is None):
        raise ValueError("exactly one root selector required")
    result = MuxOperation.record["result"]
    run_id = func.coalesce(result["permissions"]["fleet"]["run_id"].astext, result["run_id"].astext)
    original = func.coalesce(result["root_origin_id"].astext, result["origin_id"].astext)
    selector = (
        result["origin_id"].astext == str(origin_id)
        if origin_id is not None
        else result["intent_id"].astext == str(intent_id)
    )
    return (
        await session.scalars(
            select(FleetRun.deadline_at)
            .join(MuxOperation, cast(FleetRun.id, String) == run_id)
            .where(
                MuxOperation.key.startswith("fleet_root:"),
                MuxOperation.status == "processed",
                FleetRun.tenant_id == MuxOperation.tenant_id,
                cast(FleetRun.orchestrator_account_id, String) == MuxOperation.account_id,
                FleetRun.operation_key == "fleet_root:" + original,
                FleetRun.status.in_(("running", "collecting")),
                FleetRun.deadline_at > now,
                selector,
            )
        )
    ).one_or_none()


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
    expected_status: OperationStatus | None = None,
    expected_updated_at: datetime | None = None,
) -> OperationRecord:
    record = await _owned(session, scope, key)
    await _check_fence(session, record.slot, fence, clock)
    if result is not None and "root_finance" in record.result:
        result = {
            **result,
            **{name: record.result[name] for name in ("root_controls", "root_finance")},
        }
    # Under the row lock `_owned` took, so the expected-state check is atomic.
    advanced = operations.advance(
        record,
        status,
        now=now,
        resource=resource,
        result=result,
        expected_status=expected_status,
        expected_updated_at=expected_updated_at,
    )
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
    session: AsyncSession,
    session_id: str,
    *,
    after: int = -1,
    limit: int = 1000,
    read_persisted: bool = False,
) -> list[Event]:
    statement = (
        select(Journal.event)
        .where(Journal.session_id == session_id, Journal.sequence > after)
        .order_by(Journal.sequence)
        .limit(limit)
    )
    bodies = (
        (await read_persisted_columns(session, statement)).scalars()
        if read_persisted
        else await session.scalars(statement)
    )
    return [Event.model_validate(body) for body in bodies]


async def projection(
    session: AsyncSession, session_id: str, *, read_persisted: bool = False
) -> ProjectionSnapshot | None:
    statement = select(JournalSession.projection).where(JournalSession.session_id == session_id)
    body = (
        (await read_persisted_columns(session, statement)).scalar_one_or_none()
        if read_persisted
        else await session.scalar(statement)
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
        before_claim: BeforeClaim | None = None,
    ) -> None:
        self._factory = session_factory
        self._trust_caller_clock = trust_caller_clock
        self._before_claim = before_claim
        self._active_check: ContextVar[BeforeClaim | None] = ContextVar(
            f"before_claim_{id(self)}", default=None
        )

    def owns_sessions(self, session_factory: async_sessionmaker[AsyncSession]) -> bool:
        """Whether journal claims use the host's actual transaction owner."""
        return self._factory is session_factory

    @contextmanager
    def checking_claims(self, check: BeforeClaim, *, compose: bool = False) -> Iterator[None]:
        """Run `check` before this store's send claims, in this task only.

        For the block's duration, claims made in the current task (and in tasks
        it starts) run `check` instead of the constructor's. Other tasks sharing
        this same store keep theirs, so concurrent workers never see each other's.
        """
        previous = self._active_check.get()
        previous = previous if previous is not None else self._before_claim
        if compose and previous is not None:

            async def combined(
                session: AsyncSession, scope: Scope, key: str, fence: Lease | None
            ) -> bool:
                return await previous(session, scope, key, fence) and await check(
                    session, scope, key, fence
                )

            selected = combined
        else:
            selected = check
        token = self._active_check.set(selected)
        try:
            yield
        finally:
            self._active_check.reset(token)

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

    async def get_binding(
        self, slot: Slot, *, include_released: bool = False
    ) -> BindingContract | None:
        async with self._tx() as session:
            return await get_binding(session, slot, include_released=include_released)

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
        active = self._active_check.get()
        check = active if active is not None else self._before_claim
        async with self._tx() as session:
            if check is None or await check(session, scope, key, fence):
                return await claim_send(
                    session, scope, key, now=now, clock=self._clock(now), fence=fence
                )
        # Refused: what the check wrote (its evidence) is committed, nothing is claimed.
        raise ScopeViolation(key, "the send was refused before its claim")

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
        expected_status: OperationStatus | None = None,
        expected_updated_at: datetime | None = None,
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
                expected_status=expected_status,
                expected_updated_at=expected_updated_at,
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


async def next_config_local(session: AsyncSession, channel: ChannelRef) -> int:
    """Allocate under the caller's existing tenant policy write fence."""
    latest = await session.scalar(
        select(func.max(ChannelConfigRevision.local)).where(*_channel_where(channel))
    )
    return int(latest or 0) + 1


async def effective_revision(
    session: AsyncSession,
    channel: ChannelRef,
    digest: str,
    origin: str,
    *,
    agent_revision: AgentBackendRevision | None = None,
) -> ConfigRevision | None:
    reference = _agent_backend_reference(channel, origin, agent_revision)
    body = await session.scalar(
        select(ChannelConfigRevision.revision)
        .where(
            *_channel_where(channel),
            ChannelConfigRevision.origin == origin,
            ChannelConfigRevision.digest == digest,
            ChannelConfigRevision.agent_backend_ref == reference
            if reference is not None
            else ChannelConfigRevision.agent_backend_ref.is_(None),
        )
        .order_by(ChannelConfigRevision.local.desc())
        .limit(1)
    )
    return ConfigRevision.model_validate(body) if body is not None else None


async def config_origin(session: AsyncSession, channel: ChannelRef, local: int) -> str | None:
    return await session.scalar(
        select(ChannelConfigRevision.origin).where(
            *_channel_where(channel), ChannelConfigRevision.local == local
        )
    )


async def get_config_agent_revision(
    session: AsyncSession, revision: ConfigRevision, *, agent_id: uuid.UUID
) -> AgentBackendRevision | None:
    """Recover only the immutable consent association retained with this effective revision."""
    statement = select(ChannelConfigRevision.agent_backend_ref).where(
        *_channel_where(revision.channel),
        ChannelConfigRevision.local == revision.local,
        ChannelConfigRevision.origin == "agent",
    )
    raw = (await read_persisted_columns(session, statement)).scalar_one_or_none()
    if raw is None:
        return None
    try:
        identity, local = TypeAdapter(
            tuple[uuid.UUID, Annotated[int, Field(gt=0, strict=True)]]
        ).validate_python(raw)
    except ValueError:
        raise ScopeViolation(
            revision.channel.channel_id, "invalid agent backend association"
        ) from None
    if identity != agent_id:
        raise ScopeViolation(revision.channel.channel_id, "foreign agent backend association")
    retained = await agent_backends.current(
        session, _tenant(revision.channel.tenant_id), identity, local=local, persisted=True
    )
    if retained is None or retained.drop_consent is None:
        raise ScopeViolation(revision.channel.channel_id, "missing original backend consent")
    return retained
