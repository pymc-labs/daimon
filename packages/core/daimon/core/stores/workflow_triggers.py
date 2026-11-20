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
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from pydantic import BaseModel, ConfigDict
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
    listening_until: datetime | None

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


def _next_slot(spec: Mapping[str, Any], now: datetime) -> datetime | None:
    try:
        return validated_next_slot(str(spec.get("cron")), str(spec.get("timezone") or "UTC"), now)
    except InvalidScheduleError:
        return None  # validation refuses such a cron at publish; an old row just never fires


async def sync_live(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    triggers: Sequence[Mapping[str, Any]],
    now: datetime,
) -> None:
    """Make the rows match a newly published version. A schedule whose cron or zone changed
    restarts from the next slot after `now`; triggers the version dropped stop firing."""
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
                and all(old.live.get(f) == spec.get(f) for f in ("cron", "timezone"))
            )
            if not (same and old and old.next_fire_at):
                values |= {"next_fire_at": _next_slot(spec, now), "seeded_after": now}
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
        fresh = slot >= now - max_age or spec.get("catch_up") == "run-once"
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
    test: bool = False,
    verified: bool = False,
    http_status: int | None = None,
    reason: str | None = None,
    headers: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> tuple[EventRow, EventRow | None]:
    """The event, and the first one with its delivery id when it is a repeat (recorded as a
    duplicate, with no payload)."""
    values: dict[str, Any] = {
        "tenant_id": trigger.tenant_id,
        "trigger_id": trigger.id,
        "delivery_id": delivery_id,
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
        .on_conflict_do_nothing(
            index_elements=["trigger_id", "delivery_id"],
            index_where=(WorkflowTriggerEvent.delivery_id.is_not(None))
            & (WorkflowTriggerEvent.status != "duplicate"),
        )
        .returning(WorkflowTriggerEvent)
    )
    if inserted is not None:
        return EventRow.model_validate(inserted), None
    first = await session.scalar(
        select(WorkflowTriggerEvent).where(
            WorkflowTriggerEvent.trigger_id == trigger.id,
            WorkflowTriggerEvent.delivery_id == delivery_id,
            WorkflowTriggerEvent.status != "duplicate",
        )
    )
    assert first is not None, "a conflict means the first one is there"
    values |= {"status": "duplicate", "payload": None, "inputs": None, "http_status": 200}
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
    """Events of one trigger, or of every trigger of a workspace, since `since`."""
    stmt = select(func.count()).where(WorkflowTriggerEvent.received_at >= since)
    if trigger_id is not None:
        stmt = stmt.where(WorkflowTriggerEvent.trigger_id == trigger_id)
    if tenant_id is not None:
        # Through the triggers, so the (trigger, received) index serves it.
        ids = select(WorkflowTrigger.id).where(WorkflowTrigger.tenant_id == tenant_id)
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
    """The deliveries log keeps the payload retention's days; older events go."""
    gone = await session.scalars(
        delete(WorkflowTriggerEvent)
        .where(WorkflowTriggerEvent.received_at < before)
        .returning(WorkflowTriggerEvent.id)
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
