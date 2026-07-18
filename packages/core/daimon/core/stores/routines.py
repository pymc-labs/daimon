"""Routines store — CRUD plus concurrency-safe `claim_due_fireable` and
`advance_stale` helpers.

`claim_due_fireable` is a 2-step null-out: step 1 atomically claims a
batch of due rows by `UPDATE ... WHERE id IN (SELECT id ... FOR UPDATE
SKIP LOCKED LIMIT N)` and zeroes their `next_fire_at` (Postgres has no
direct `UPDATE ... LIMIT`, hence the subquery). Step 2 walks the
returned rows and recomputes `next_fire_at` per row from cron. A crash
between the two phases leaves an orphan (`next_fire_at IS NULL`); the
companion `advance_stale` call recovers those plus any rows whose
`next_fire_at` slipped past the freshness cutoff.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta
from typing import Any, cast

import structlog
from daimon.core._models import Routine
from daimon.core.cron import next_slot_at_or_after
from daimon.core.stores.domain import RoutineRow
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def create_routine(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    created_by_user_id: str | None,
    agent_id: str,
    agent_name: str,
    cron_expr: str,
    timezone_: str,
    trigger_message: str,
    enabled: bool = True,
    next_fire_at: datetime | None = None,
) -> RoutineRow:
    orm = Routine(
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone=timezone_,
        trigger_message=trigger_message,
        enabled=enabled,
        next_fire_at=next_fire_at,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return RoutineRow.model_validate(orm)


async def get_routine(session: AsyncSession, routine_id: _uuid.UUID) -> RoutineRow | None:
    orm = (
        await session.execute(select(Routine).where(Routine.id == routine_id))
    ).scalar_one_or_none()
    if orm is None:
        return None
    return RoutineRow.model_validate(orm)


async def list_routines_missing_agent_name(
    session: AsyncSession,
) -> list[RoutineRow]:
    """Return all routines where `agent_name IS NULL`.

    Used by the one-shot `daimon routines backfill-agent-names` CLI command
    Inherently idempotent: re-running after backfill returns
    an empty list. No pagination — the row count is bounded by tenant
    routine cardinality, which is small.
    """
    rows = (
        (await session.execute(select(Routine).where(Routine.agent_name.is_(None)))).scalars().all()
    )
    return [RoutineRow.model_validate(r) for r in rows]


async def list_routines_for_tenant(
    session: AsyncSession, *, tenant_id: _uuid.UUID
) -> list[RoutineRow]:
    rows = (
        (await session.execute(select(Routine).where(Routine.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    return [RoutineRow.model_validate(r) for r in rows]


async def update_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    cron_expr: str | None = None,
    timezone_: str | None = None,
    trigger_message: str | None = None,
    enabled: bool | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    next_fire_at: datetime | None = None,
) -> RoutineRow | None:
    values: dict[str, str | bool | datetime] = {}
    if cron_expr is not None:
        values["cron_expr"] = cron_expr
    if timezone_ is not None:
        values["timezone"] = timezone_
    if trigger_message is not None:
        values["trigger_message"] = trigger_message
    if enabled is not None:
        values["enabled"] = enabled
    if agent_id is not None:
        values["agent_id"] = agent_id
    if agent_name is not None:
        values["agent_name"] = agent_name
    if next_fire_at is not None:
        values["next_fire_at"] = next_fire_at
    if not values:
        return await get_routine(session, routine_id)

    stmt = (
        update(Routine)
        .where(Routine.id == routine_id)
        .values(**values)
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        return None
    await session.flush()
    return RoutineRow.model_validate(orm)


async def update_routine_agent_id(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    new_agent_id: str,
) -> bool:
    """Update only ``routines.agent_id``. Used by the scheduler resolver path
    when the resolver heals a stale id. Does NOT touch
    ``next_fire_at``, ``cron_expr``, ``agent_name``, etc.

    Returns True if a row was updated, False otherwise.
    """
    result = await session.execute(
        update(Routine).where(Routine.id == routine_id).values(agent_id=new_agent_id)
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def delete_routine(session: AsyncSession, routine_id: _uuid.UUID) -> bool:
    result = await session.execute(delete(Routine).where(Routine.id == routine_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount > 0


async def delete_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> int:
    """Delete all routines created by `(tenant_id, external_id)`. Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.
    `created_by_user_id` is a Text column holding the platform `external_id`.
    """
    result = await session.execute(
        delete(Routine).where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_routines_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> int:
    """Count routines that `delete_for_principal` would delete. Read-only."""
    stmt = (
        select(func.count())
        .select_from(Routine)
        .where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def get_first_routine_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> RoutineRow | None:
    """Return the first routine row for `(tenant_id, external_id)`, or None.

    Used for human-display "example" labels in /privacy cascade previews.
    Ordered by `created_at` so the example is stable.
    """
    stmt = (
        select(Routine)
        .where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
        .order_by(Routine.created_at)
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else RoutineRow.model_validate(orm)


async def pause_routine(session: AsyncSession, routine_id: _uuid.UUID) -> RoutineRow | None:
    """Set ``enabled=False`` and ``next_fire_at=NULL`` atomically.

    Returns the updated row, or ``None`` if no row matched ``routine_id``.
    """
    stmt = (
        update(Routine)
        .where(Routine.id == routine_id)
        .values(enabled=False, next_fire_at=None)
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    await session.flush()
    return RoutineRow.model_validate(orm)


async def resume_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    now: datetime,
) -> RoutineRow | None:
    """Set ``enabled=True`` and ``next_fire_at`` to the next cron slot at-or-after ``now``.

    Caller injects ``now`` so this helper stays clock-free. Returns the
    updated row, or ``None`` if no row matched ``routine_id``.
    """
    row = await get_routine(session, routine_id)
    if row is None:
        return None
    nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
    stmt = (
        update(Routine)
        .where(Routine.id == routine_id)
        .values(enabled=True, next_fire_at=nxt)
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    await session.flush()
    return RoutineRow.model_validate(orm)


async def record_result(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tail: str | None,
    error: str | None,
) -> None:
    """Set `last_result_tail` and `last_error` in a single UPDATE.

    `error=None` clears `last_error` (sets NULL); `error="..."` sets it.
    `tail` is written as-is (None or str).
    """
    await session.execute(
        update(Routine)
        .where(Routine.id == routine_id)
        .values(last_result_tail=tail, last_error=error)
    )
    await session.flush()


async def claim_due_fireable(
    session: AsyncSession,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
    limit: int = 20,
) -> list[RoutineRow]:
    """Step 1: atomically claim due rows. Step 2: recompute next_fire_at.

    Returned rows reflect the row state BEFORE step-2 recompute (matches
    predecessor): callers see `next_fire_at=None` and `last_fired_at=now`
    on each claimed row, while the table itself has the freshly-computed
    next slot stamped per row.
    """
    window_start = now - max_age

    # Postgres does not support `UPDATE ... LIMIT`, so we select the ids in a
    # subquery with FOR UPDATE SKIP LOCKED + LIMIT, then UPDATE the outer set.
    id_subq = (
        select(Routine.id)
        .where(
            Routine.enabled.is_(True),
            Routine.next_fire_at.is_not(None),
            Routine.next_fire_at >= window_start,
            Routine.next_fire_at <= now,
        )
        .order_by(Routine.next_fire_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    claim_stmt = (
        update(Routine)
        .where(Routine.id.in_(id_subq))
        .values(next_fire_at=None, last_fired_at=now)
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    claimed_orm = (await session.execute(claim_stmt)).scalars().all()
    claimed_rows = [RoutineRow.model_validate(r) for r in claimed_orm]

    # Step 2: per-row recompute. One bad routine must not block the others —
    # this is the documented predecessor pattern (named boundary catch).
    for row in claimed_rows:
        try:
            nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
            await session.execute(
                update(Routine).where(Routine.id == row.id).values(next_fire_at=nxt)
            )
        except Exception:
            log.exception("claim_due_fireable step-2 recompute failed", routine_id=str(row.id))

    await session.flush()
    return claimed_rows


async def advance_stale(
    session: AsyncSession,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
    limit: int = 200,
) -> int:
    """Recover stale (next_fire_at < now-max_age) and orphan (NULL) rows.

    Recomputes `next_fire_at` per row. Returns count touched.
    """
    cutoff = now - max_age
    stmt = (
        select(Routine)
        .where(
            Routine.enabled.is_(True),
            or_(
                Routine.next_fire_at < cutoff,
                Routine.next_fire_at.is_(None),
            ),
        )
        .limit(limit)
    )
    rows_orm = (await session.execute(stmt)).scalars().all()
    rows = [RoutineRow.model_validate(r) for r in rows_orm]

    touched = 0
    for row in rows:
        try:
            nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
            await session.execute(
                update(Routine).where(Routine.id == row.id).values(next_fire_at=nxt)
            )
            touched += 1
        except Exception:
            log.exception("advance_stale recompute failed", routine_id=str(row.id))

    await session.flush()
    return touched
