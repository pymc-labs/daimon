"""Transactional operator-only routine handover operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from daimon.core._models import Routine, Tenant
from daimon.core.errors import StoreError
from daimon.core.routine_move import RoutineSeedEntry, seed_first_slot
from daimon.core.stores.domain import RoutineRow
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def seed_routines(
    session: AsyncSession,
    entries: list[RoutineSeedEntry],
    *,
    tenant_id: UUID,
    now: datetime,
    apply: bool = False,
) -> list[tuple[RoutineRow, datetime]]:
    """Caller owns one transaction; lock and validate the whole batch before writes.

    The guarded update atomically enables and seeds each paused row. Row locks
    also serialize ordinary edits/resume and concurrent seed attempts.
    """
    errors: list[str] = []
    ids = [entry.internal_routine_id for entry in entries]
    seen: set[UUID] = set()
    for rid in ids:
        if rid in seen:
            errors.append(f"{rid}: duplicate routine id")
        seen.add(rid)
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.archived_at.is_(None))
    )
    if tenant is None:
        raise StoreError("Select an active tenant. Nothing was changed.")
    statement = (
        select(Routine)
        .where(Routine.tenant_id == tenant_id, Routine.internal_routine_id.in_(ids))
        .order_by(Routine.id)
    )
    if apply:
        statement = statement.with_for_update()
    rows: dict[UUID, list[RoutineRow]] = {}
    for orm in (await session.scalars(statement.execution_options(populate_existing=True))).all():
        assert orm.internal_routine_id is not None
        rows.setdefault(orm.internal_routine_id, []).append(RoutineRow.model_validate(orm))
    planned: list[tuple[RoutineRow, datetime]] = []
    for entry in entries:
        matches = rows.get(entry.internal_routine_id, [])
        if len(matches) != 1:
            reason = "no routine in this tenant" if not matches else "several routines match"
            errors.append(f"{entry.internal_routine_id}: {reason}")
            continue
        row = matches[0]
        try:
            planned.append((row, seed_first_slot(entry, row, now=now)))
        except ValueError as exc:
            errors.append(f"{entry.internal_routine_id}: {exc}")
    if errors:
        raise StoreError("\n".join(errors) + "\nNothing was changed.")
    if apply:
        seeds = {entry.internal_routine_id: entry.last_completed_slot for entry in entries}
        for row, slot in planned:
            assert row.internal_routine_id is not None
            saved = await session.scalar(
                update(Routine)
                .where(
                    Routine.id == row.id,
                    Routine.tenant_id == tenant_id,
                    Routine.internal_routine_id == row.internal_routine_id,
                    Routine.enabled.is_(False),
                    Routine.last_fired_at.is_(None),
                    Routine.last_fired_slot.is_(None),
                    Routine.updated_at == row.updated_at,
                )
                .values(
                    next_fire_at=slot,
                    enabled=True,
                    seeded_after=seeds[row.internal_routine_id],
                    updated_at=func.greatest(
                        func.clock_timestamp(), Routine.updated_at + timedelta(microseconds=1)
                    ),
                )
                .returning(Routine.id)
            )
            if saved is None:
                raise StoreError(
                    f"{row.internal_routine_id}: routine changed during seed. Nothing was changed."
                )
    return planned


async def rollback_rows(
    session: AsyncSession, *, tenant_id: UUID, internal_routine_ids: list[UUID] | None = None
) -> list[RoutineRow]:
    """One SELECT gives a consistent snapshot without locks or database writes."""
    statement = select(Routine).where(Routine.tenant_id == tenant_id).order_by(Routine.id)
    if internal_routine_ids is not None:
        if len(set(internal_routine_ids)) != len(internal_routine_ids):
            raise StoreError("Duplicate routine id in rollback selection.")
        statement = statement.where(Routine.internal_routine_id.in_(internal_routine_ids))
    rows = [
        RoutineRow.model_validate(row)
        for row in (
            await session.scalars(statement.execution_options(populate_existing=True))
        ).all()
    ]
    if internal_routine_ids is not None:
        missing = set(internal_routine_ids) - {row.internal_routine_id for row in rows}
        if missing:
            raise StoreError(
                "\n".join(f"{rid}: no routine in this tenant" for rid in sorted(missing))
            )
    return rows
