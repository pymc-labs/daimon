"""Store for workflow triggers: their rows, schedule slots, webhook secrets and events.

A row exists per trigger id a workflow's draft or live version names. `config` holds
`spec`, the live version's trigger (absent while the trigger is only in the draft), and
`listener`, the editor whose Listen was last on. Events are what the starts loop turns
into runs: webhook deliveries, schedule slots and other workflows' finished runs. A
`(trigger, delivery id)` pair is unique among events that are not duplicates, so a slot
or delivery seen twice never starts two runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from daimon.core._models import (
    Tenant,
    Workflow,
    WorkflowDelivery,
    WorkflowRun,
    WorkflowTrigger,
    WorkflowTriggerEvent,
    WorkflowTriggerSecret,
)
from daimon.core.cron import InvalidScheduleError
from daimon.core.workflows.clock import next_slot
from daimon.core.workflows.spec import ScheduleTrigger, parse_duration
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

EventStatus = Literal["accepted", "started", "skipped", "rejected", "duplicate"]


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class TriggerRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    trigger_key: str
    kind: str
    config: dict[str, Any]
    enabled: bool
    next_fire_at: datetime | None
    last_fired_slot: datetime | None
    seeded_after: datetime | None
    listening_until: datetime | None
    from_routine_id: uuid.UUID | None
    places: list[str] | None
    state: dict[str, Any] | None

    @property
    def live(self) -> dict[str, Any] | None:
        """The live version's trigger, or None when only the draft has it."""
        spec = self.config.get("spec")
        return spec if isinstance(spec, dict) else None  # pyright: ignore[reportUnknownVariableType]  # JSONB

    @property
    def listener(self) -> uuid.UUID | None:
        value = self.config.get("listener")
        return uuid.UUID(value) if isinstance(value, str) else None


class SecretRow(_Row):
    id: uuid.UUID
    encrypted_secret: bytes
    hint: str
    created_at: datetime
    expires_at: datetime | None


class EventRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    trigger_id: uuid.UUID
    delivery_id: str | None
    test: bool
    verified: bool
    status: str
    http_status: int | None
    reason: str | None
    headers: dict[str, Any] | None
    payload: dict[str, Any] | None
    inputs: dict[str, Any] | None
    run_id: uuid.UUID | None
    received_at: datetime


@dataclass(frozen=True)
class Cursor:
    """A schedule's place taken over from a routine (or handed back to one)."""

    routine_id: uuid.UUID
    next_fire_at: datetime | None
    last_fired_slot: datetime | None
    seeded_after: datetime | None
    #: Kept as `config.routine`: the routine's name and last result before the move.
    snapshot: Mapping[str, Any] | None = None


_SLOT_FIELDS = ("cron", "timezone", "every_weeks", "anchor", "at")


def _next_slot(spec: Mapping[str, Any], now: datetime) -> datetime | None:
    try:
        return next_slot(ScheduleTrigger.model_validate(spec), now)
    except (InvalidScheduleError, ValidationError):
        return None  # validation refuses such a schedule at publish; an old row just never fires


async def sync_live(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    triggers: Sequence[Mapping[str, Any]],
    now: datetime,
    cursors: Mapping[str, Cursor] | None = None,
) -> None:
    """Make the rows match a newly published version. A schedule whose cron, zone, weeks or
    time changed restarts from the next slot after `now`; triggers the version dropped stop firing.
    A schedule in `cursors` carries on from a routine's slot state and stays linked to it."""
    rows = {r.trigger_key: r for r in await list_triggers(session, workflow_id=workflow_id)}
    for spec in triggers:
        key, kind = str(spec["id"]), str(spec["kind"])
        old = rows.pop(key, None)
        values: dict[str, Any] = {
            "kind": kind,
            "config": {**(old.config if old else {}), "spec": dict(spec)},
        }
        if kind == "schedule":
            same = (
                old is not None
                and old.live is not None
                and all(old.live.get(f) == spec.get(f) for f in _SLOT_FIELDS)
            )
            if not (same and old and old.next_fire_at):
                values |= {"next_fire_at": _next_slot(spec, now), "seeded_after": now}
            if (cursor := (cursors or {}).get(key)) is not None:
                if cursor.snapshot is not None:
                    values["config"] = {**values["config"], "routine": dict(cursor.snapshot)}
                values |= {
                    "from_routine_id": cursor.routine_id,
                    "next_fire_at": cursor.next_fire_at or values.get("next_fire_at"),
                    "last_fired_slot": cursor.last_fired_slot,
                    "seeded_after": cursor.seeded_after or values.get("seeded_after"),
                }
        elif kind == "poll":
            # A changed poll starts over (a new baseline); a republish of a paused one wakes it.
            if old is None or old.live != spec:
                values |= {"state": {}, "enabled": True, "next_fire_at": now}
            elif not old.enabled:
                kept = {k: v for k, v in (old.state or {}).items() if k not in ("paused", "digest")}
                values |= {"state": kept, "enabled": True, "next_fire_at": now}
        else:
            values["next_fire_at"] = None
        if old is None:
            await session.execute(
                insert(WorkflowTrigger).values(
                    tenant_id=tenant_id, workflow_id=workflow_id, trigger_key=key, **values
                )
            )
        else:
            await session.execute(
                update(WorkflowTrigger).where(WorkflowTrigger.id == old.id).values(**values)
            )
    for gone in rows.values():
        config = {k: v for k, v in gone.config.items() if k != "spec"}
        await session.execute(
            update(WorkflowTrigger)
            .where(WorkflowTrigger.id == gone.id)
            .values(config=config, next_fire_at=None)
        )


async def list_triggers(session: AsyncSession, *, workflow_id: uuid.UUID) -> list[TriggerRow]:
    rows = await session.scalars(
        select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == workflow_id)
    )
    return [TriggerRow.model_validate(r) for r in rows]


async def get_trigger(
    session: AsyncSession, trigger_id: uuid.UUID, *, lock: bool = False
) -> TriggerRow | None:
    """By id alone: the webhook URL carries only the id, and the row names its workspace."""
    stmt = select(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id)
    orm = (await session.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    return None if orm is None else TriggerRow.model_validate(orm)


async def ensure_trigger(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, key: str, kind: str
) -> TriggerRow:
    """The row for a trigger the draft names, made when first asked for."""
    await session.execute(
        insert(WorkflowTrigger)
        .values(tenant_id=tenant_id, workflow_id=workflow_id, trigger_key=key, kind=kind)
        .on_conflict_do_nothing(constraint="uq_workflow_triggers_key")
    )
    orm = await session.scalar(
        select(WorkflowTrigger).where(
            WorkflowTrigger.workflow_id == workflow_id,
            WorkflowTrigger.tenant_id == tenant_id,
            WorkflowTrigger.trigger_key == key,
        )
    )
    assert orm is not None, "the row was just made"
    return TriggerRow.model_validate(orm)


async def set_listening(
    session: AsyncSession,
    trigger: TriggerRow,
    *,
    until: datetime | None,
    listener: uuid.UUID | None,
) -> None:
    config = dict(trigger.config)
    if listener is not None:
        config["listener"] = str(listener)
    await session.execute(
        update(WorkflowTrigger)
        .where(WorkflowTrigger.id == trigger.id)
        .values(listening_until=until, config=config)
    )


# --- schedules ---


async def get_routine_trigger(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> TriggerRow | None:
    """The workflow's trigger that a routine moved into, locked until the transaction ends."""
    orm = await session.scalar(
        select(WorkflowTrigger)
        .where(
            WorkflowTrigger.tenant_id == tenant_id,
            WorkflowTrigger.workflow_id == workflow_id,
            WorkflowTrigger.from_routine_id.is_not(None),
        )
        .with_for_update()
    )
    return None if orm is None else TriggerRow.model_validate(orm)


async def release_to_routine(session: AsyncSession, trigger: TriggerRow) -> None:
    """Unlink a trigger whose routine took its cursor back. It has no next slot until the
    workflow publishes again, so resuming the paused workflow alone fires nothing twice."""
    await session.execute(
        update(WorkflowTrigger)
        .where(WorkflowTrigger.id == trigger.id)
        .values(from_routine_id=None, next_fire_at=None)
    )


async def claim_due_schedules(
    session: AsyncSession, *, now: datetime, max_age: timedelta, limit: int
) -> list[tuple[TriggerRow, datetime]]:
    """Due schedule slots of active, published workflows, locked SKIP LOCKED: each row moves
    to its next slot after `now`. A slot older than `max_age` fires only under
    `catch_up: run-once`; otherwise it is skipped. Returns `(trigger, slot)` that fire."""
    rows = await session.scalars(
        select(WorkflowTrigger)
        .join(Workflow, Workflow.id == WorkflowTrigger.workflow_id)
        .join(Tenant, Tenant.id == WorkflowTrigger.tenant_id)
        .where(
            WorkflowTrigger.kind == "schedule",
            WorkflowTrigger.enabled.is_(True),
            WorkflowTrigger.next_fire_at <= now,
            Workflow.status == "active",
            Workflow.live_version.is_not(None),
            Tenant.archived_at.is_(None),
        )
        .order_by(WorkflowTrigger.next_fire_at)
        .limit(limit)
        .with_for_update(of=WorkflowTrigger, skip_locked=True)
    )
    fired: list[tuple[TriggerRow, datetime]] = []
    for orm in rows.all():
        row = TriggerRow.model_validate(orm)
        slot, spec = row.next_fire_at, row.live
        assert slot is not None, "due rows have a slot"
        if spec is None:
            continue  # sync_live clears the slot of a dropped trigger; nothing is due
        # A one-time `at` fires even when late, once: its next slot is none.
        fresh = slot >= now - max_age or spec.get("catch_up") == "run-once" or "at" in spec
        await session.execute(
            update(WorkflowTrigger)
            .where(WorkflowTrigger.id == row.id)
            .values(
                next_fire_at=_next_slot(spec, now),
                last_fired_slot=slot if fresh else row.last_fired_slot,
            )
        )
        if fresh:
            fired.append((row, slot))
    return fired


# --- polls ---


async def claim_due_polls(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[tuple[TriggerRow, timedelta]]:
    """Due poll triggers of active, published workflows, locked SKIP LOCKED: each moves to its
    next check at once, so a poll that fails or hangs is retried an interval later, never
    in a tight loop. Returns each with its interval."""
    rows = await session.scalars(
        select(WorkflowTrigger)
        .join(Workflow, Workflow.id == WorkflowTrigger.workflow_id)
        .join(Tenant, Tenant.id == WorkflowTrigger.tenant_id)
        .where(
            WorkflowTrigger.kind == "poll",
            WorkflowTrigger.enabled.is_(True),
            WorkflowTrigger.next_fire_at <= now,
            Workflow.status == "active",
            Workflow.live_version.is_not(None),
            Tenant.archived_at.is_(None),
        )
        .order_by(WorkflowTrigger.next_fire_at)
        .limit(limit)
        .with_for_update(of=WorkflowTrigger, skip_locked=True)
    )
    due: list[tuple[TriggerRow, timedelta]] = []
    for orm in rows.all():
        row = TriggerRow.model_validate(orm)
        spec = row.live
        if spec is None:
            continue  # sync_live clears the slot of a dropped trigger
        every = parse_duration(str(spec["every"]))
        await session.execute(
            update(WorkflowTrigger)
            .where(WorkflowTrigger.id == row.id)
            .values(next_fire_at=now + every)
        )
        due.append((row, every))
    return due


async def set_poll_state(
    session: AsyncSession, trigger_id: uuid.UUID, state: Mapping[str, Any]
) -> None:
    await session.execute(
        update(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id).values(state=dict(state))
    )


async def pause_poll(session: AsyncSession, trigger: TriggerRow, *, reason: str, text: str) -> None:
    """Stop a poll that cannot go on as approved; the owner hears and a republish wakes it."""
    state = {**(trigger.state or {}), "paused": reason}
    await session.execute(
        update(WorkflowTrigger)
        .where(WorkflowTrigger.id == trigger.id)
        .values(enabled=False, state=state)
    )
    await add_owner_notice(
        session,
        tenant_id=trigger.tenant_id,
        workflow_id=trigger.workflow_id,
        topic=f"poll_paused:{trigger.trigger_key}",
        text=text,
    )


# --- secrets ---


async def add_secret(
    session: AsyncSession,
    *,
    trigger: TriggerRow,
    encrypted: bytes,
    hint: str,
    created_by: uuid.UUID | None,
    now: datetime,
    overlap: timedelta,
) -> SecretRow:
    """A new current secret; the ones before it keep working for `overlap` at most."""
    until = now + overlap
    await session.execute(
        update(WorkflowTriggerSecret)
        .where(
            WorkflowTriggerSecret.trigger_id == trigger.id,
            or_(
                WorkflowTriggerSecret.expires_at.is_(None), WorkflowTriggerSecret.expires_at > until
            ),
        )
        .values(expires_at=until)
    )
    orm = WorkflowTriggerSecret(
        tenant_id=trigger.tenant_id,
        trigger_id=trigger.id,
        encrypted_secret=encrypted,
        hint=hint,
        created_by_account_id=created_by,
        created_at=now,
    )
    session.add(orm)
    await session.flush()
    return SecretRow.model_validate(orm)


async def live_secrets(
    session: AsyncSession, *, trigger_id: uuid.UUID, now: datetime
) -> list[SecretRow]:
    """Secrets that still verify, the newest first."""
    rows = await session.scalars(
        select(WorkflowTriggerSecret)
        .where(
            WorkflowTriggerSecret.trigger_id == trigger_id,
            or_(WorkflowTriggerSecret.expires_at.is_(None), WorkflowTriggerSecret.expires_at > now),
        )
        .order_by(WorkflowTriggerSecret.created_at.desc())
    )
    return [SecretRow.model_validate(r) for r in rows]


# --- events ---


async def record_event(
    session: AsyncSession,
    *,
    trigger: TriggerRow,
    status: EventStatus,
    received_at: datetime,
    delivery_id: str | None = None,
    payload_hash: str | None = None,
    test: bool = False,
    verified: bool = False,
    http_status: int | None = None,
    reason: str | None = None,
    headers: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> tuple[EventRow, EventRow | None]:
    """The event, and the first one with its delivery id or payload hash when it is a repeat
    (recorded as a duplicate, with no payload)."""
    values: dict[str, Any] = {
        "tenant_id": trigger.tenant_id,
        "trigger_id": trigger.id,
        "delivery_id": delivery_id,
        "payload_hash": payload_hash,
        "test": test,
        "verified": verified,
        "status": status,
        "http_status": http_status,
        "reason": reason,
        "headers": headers,
        "payload": payload,
        "inputs": inputs,
        "received_at": received_at,
    }
    inserted = await session.scalar(
        insert(WorkflowTriggerEvent)
        .values(**values)
        # Either dedupe index: a repeated delivery id or payload hash.
        .on_conflict_do_nothing()
        .returning(WorkflowTriggerEvent)
    )
    if inserted is not None:
        return EventRow.model_validate(inserted), None
    keys = [
        column == value
        for column, value in (
            (WorkflowTriggerEvent.delivery_id, delivery_id),
            (WorkflowTriggerEvent.payload_hash, payload_hash),
        )
        if value is not None
    ]
    first = await session.scalar(
        select(WorkflowTriggerEvent)
        .where(
            WorkflowTriggerEvent.trigger_id == trigger.id,
            or_(*keys),
            WorkflowTriggerEvent.status != "duplicate",
        )
        .limit(1)
    )
    assert first is not None, "a conflict means the first one is there"
    values |= {
        "status": "duplicate",
        "payload": None,
        "payload_hash": None,
        "inputs": None,
        "http_status": 200,
    }
    dup = await session.scalar(
        insert(WorkflowTriggerEvent).values(**values).returning(WorkflowTriggerEvent)
    )
    assert dup is not None
    return EventRow.model_validate(dup), EventRow.model_validate(first)


async def count_events_since(
    session: AsyncSession,
    *,
    since: datetime,
    trigger_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Events of one trigger, or of every webhook trigger of a workspace, since `since`."""
    stmt = select(func.count()).where(WorkflowTriggerEvent.received_at >= since)
    if trigger_id is not None:
        stmt = stmt.where(WorkflowTriggerEvent.trigger_id == trigger_id)
    if tenant_id is not None:
        # Through the triggers, so the (trigger, received) index serves it.
        ids = select(WorkflowTrigger.id).where(
            WorkflowTrigger.tenant_id == tenant_id, WorkflowTrigger.kind == "webhook"
        )
        stmt = stmt.where(WorkflowTriggerEvent.trigger_id.in_(ids))
    return await session.scalar(stmt) or 0


async def list_events(
    session: AsyncSession, *, trigger_id: uuid.UUID, limit: int
) -> list[EventRow]:
    rows = await session.scalars(
        select(WorkflowTriggerEvent)
        .where(WorkflowTriggerEvent.trigger_id == trigger_id)
        .order_by(WorkflowTriggerEvent.received_at.desc())
        .limit(limit)
    )
    return [EventRow.model_validate(r) for r in rows]


async def get_event(
    session: AsyncSession, *, trigger_id: uuid.UUID, event_id: uuid.UUID
) -> EventRow | None:
    orm = await session.scalar(
        select(WorkflowTriggerEvent).where(
            WorkflowTriggerEvent.id == event_id, WorkflowTriggerEvent.trigger_id == trigger_id
        )
    )
    return None if orm is None else EventRow.model_validate(orm)


async def claim_accepted_events(
    session: AsyncSession, *, limit: int
) -> list[tuple[EventRow, TriggerRow]]:
    """Accepted events with their triggers, locked SKIP LOCKED, oldest first."""
    rows = await session.execute(
        select(WorkflowTriggerEvent, WorkflowTrigger)
        .join(WorkflowTrigger, WorkflowTrigger.id == WorkflowTriggerEvent.trigger_id)
        .where(WorkflowTriggerEvent.status == "accepted")
        .order_by(WorkflowTriggerEvent.received_at)
        .limit(limit)
        .with_for_update(of=WorkflowTriggerEvent, skip_locked=True)
    )
    return [(EventRow.model_validate(e), TriggerRow.model_validate(t)) for e, t in rows.tuples()]


async def accepted_after(session: AsyncSession, event: EventRow) -> list[EventRow]:
    """Accepted events of the same trigger received after `event` (debounce reads them)."""
    rows = await session.scalars(
        select(WorkflowTriggerEvent)
        .where(
            WorkflowTriggerEvent.trigger_id == event.trigger_id,
            WorkflowTriggerEvent.status == "accepted",
            WorkflowTriggerEvent.received_at > event.received_at,
        )
        .order_by(WorkflowTriggerEvent.received_at)
        .limit(500)
    )
    return [EventRow.model_validate(r) for r in rows]


async def mark_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    status: Literal["started", "skipped", "rejected"],
    run_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    await session.execute(
        update(WorkflowTriggerEvent)
        .where(WorkflowTriggerEvent.id == event_id)
        .values(status=status, run_id=run_id, reason=reason)
    )


async def delete_events_before(session: AsyncSession, *, before: datetime) -> int:
    """The deliveries log keeps the payload retention's days. Older events go, but a first
    delivery with a dedupe key stays, cleared, so a replay of it stays a duplicate."""
    old = WorkflowTriggerEvent.received_at < before
    keyed = (WorkflowTriggerEvent.status != "duplicate") & (
        WorkflowTriggerEvent.delivery_id.is_not(None)
        | WorkflowTriggerEvent.payload_hash.is_not(None)
    )
    gone = await session.scalars(
        delete(WorkflowTriggerEvent).where(old, ~keyed).returning(WorkflowTriggerEvent.id)
    )
    await session.execute(
        update(WorkflowTriggerEvent)
        .where(old, keyed, WorkflowTriggerEvent.payload.is_not(None))
        .values(payload=None, headers=None, inputs=None)
    )
    return len(gone.all())


async def event_triggers_for(
    session: AsyncSession, *, tenant_id: uuid.UUID, source: str
) -> list[TriggerRow]:
    """Live event triggers of the workspace's active workflows listening to `source`."""
    rows = await session.scalars(
        select(WorkflowTrigger)
        .join(Workflow, Workflow.id == WorkflowTrigger.workflow_id)
        .where(
            WorkflowTrigger.tenant_id == tenant_id,
            WorkflowTrigger.kind == "event",
            WorkflowTrigger.enabled.is_(True),
            WorkflowTrigger.config["spec"]["source"].astext == source,
            Workflow.status == "active",
        )
    )
    return [TriggerRow.model_validate(r) for r in rows]


# --- spend cap ---


async def committed_spend(
    session: AsyncSession, *, workflow_id: uuid.UUID, trigger_key: str, since: datetime
) -> Decimal:
    """What a trigger's live runs since `since` spent, counting an active run at its budget,
    so a burst of deliveries cannot outrun the cap before costs land."""
    spent = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            WorkflowRun.ended_at.is_(None),
                            func.greatest(WorkflowRun.budget_usd, WorkflowRun.cost_usd),
                        ),
                        else_=WorkflowRun.cost_usd,
                    )
                ),
                0,
            )
        ).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.trigger_key == trigger_key,
            WorkflowRun.mode == "live",
            WorkflowRun.created_at >= since,
        )
    )
    return Decimal(spent or 0)


async def notice_sent_since(
    session: AsyncSession, *, workflow_id: uuid.UUID, topic: str, since: datetime
) -> bool:
    found = await session.scalar(
        select(WorkflowDelivery.id).where(
            WorkflowDelivery.workflow_id == workflow_id,
            WorkflowDelivery.kind == "notice",
            WorkflowDelivery.payload["topic"].astext == topic,
            WorkflowDelivery.created_at >= since,
        )
    )
    return found is not None


async def add_owner_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, topic: str, text: str
) -> None:
    """A notice the delivery loop posts to the workflow's owner."""
    session.add(
        WorkflowDelivery(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            kind="notice",
            target={"to": "owner"},
            payload={"topic": topic, "text": text},
        )
    )
    await session.flush()


# --- the Triggers tab ---


async def find_trigger(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, key: str
) -> TriggerRow | None:
    orm = await session.scalar(
        select(WorkflowTrigger).where(
            WorkflowTrigger.tenant_id == tenant_id,
            WorkflowTrigger.workflow_id == workflow_id,
            WorkflowTrigger.trigger_key == key,
        )
    )
    return None if orm is None else TriggerRow.model_validate(orm)


async def run_numbers(session: AsyncSession, run_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not run_ids:
        return {}
    rows = await session.execute(
        select(WorkflowRun.id, WorkflowRun.number).where(WorkflowRun.id.in_(run_ids))
    )
    return dict(rows.tuples().all())


async def last_with_inputs(session: AsyncSession, *, workflow_id: uuid.UUID) -> EventRow | None:
    """The newest webhook delivery of the workflow that mapped to inputs."""
    orm = await session.scalar(
        select(WorkflowTriggerEvent)
        .join(WorkflowTrigger, WorkflowTrigger.id == WorkflowTriggerEvent.trigger_id)
        .where(
            WorkflowTrigger.workflow_id == workflow_id,
            WorkflowTrigger.kind == "webhook",
            func.jsonb_typeof(WorkflowTriggerEvent.inputs) == "object",
        )
        .order_by(WorkflowTriggerEvent.received_at.desc())
        .limit(1)
    )
    return None if orm is None else EventRow.model_validate(orm)


async def next_fires(
    session: AsyncSession, *, workflow_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Each workflow's next schedule slot."""
    if not workflow_ids:
        return {}
    rows = await session.execute(
        select(WorkflowTrigger.workflow_id, func.min(WorkflowTrigger.next_fire_at))
        .where(
            WorkflowTrigger.workflow_id.in_(workflow_ids),
            WorkflowTrigger.kind == "schedule",
            WorkflowTrigger.enabled.is_(True),
            WorkflowTrigger.next_fire_at.is_not(None),
        )
        .group_by(WorkflowTrigger.workflow_id)
    )
    return {w: at for w, at in rows.tuples() if at is not None}


async def routine_origins(
    session: AsyncSession, *, workflow_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, dict[str, Any]]]:
    """Each moved routine's workflow: the routine's id and its snapshot from the move."""
    if not workflow_ids:
        return {}
    rows = await session.execute(
        select(
            WorkflowTrigger.workflow_id, WorkflowTrigger.from_routine_id, WorkflowTrigger.config
        ).where(
            WorkflowTrigger.workflow_id.in_(workflow_ids),
            WorkflowTrigger.from_routine_id.is_not(None),
        )
    )
    return {w: (r, dict(c.get("routine") or {})) for w, r, c in rows.tuples() if r is not None}
