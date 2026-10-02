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
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import structlog
from daimon.core._models import Account, PlatformPrincipal, Routine, Tenant
from daimon.core.cron import next_slot_at_or_after
from daimon.core.errors import StoreError
from daimon.core.stores.domain import (
    CatchUpPolicy,
    Role,
    RoutineDestinationKind,
    RoutineRow,
    UnattendedRequester,
)
from sqlalchemy import and_, delete, false, func, or_, select, update
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
    catch_up_policy: CatchUpPolicy = "skip",
    next_fire_at: datetime | None = None,
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    channel_id: str | None = None,
) -> RoutineRow:
    """`channel_id` is the channel the routine's spend counts against (see `Routine`)."""
    if catch_up_policy not in ("skip", "run-once"):
        raise StoreError("catch_up_policy must be skip or run-once")
    _check_destination(destination_kind, destination_id)
    orm = Routine(
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone=timezone_,
        trigger_message=trigger_message,
        enabled=enabled,
        catch_up_policy=catch_up_policy,
        next_fire_at=next_fire_at,
        destination_kind=destination_kind,
        destination_id=destination_id,
        channel_id=channel_id,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return RoutineRow.model_validate(orm)


async def get_routine(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> RoutineRow | None:
    orm = (
        await session.execute(
            select(Routine).where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        )
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


async def list_routine_creators(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    agent_names: Collection[str],
    agent_id: str | None = None,
) -> list[UnattendedRequester]:
    """Who made the routines that run the agent, paused ones included.

    A routine counts when it names any of `agent_names` or runs `agent_id`.

    A routine with no recorded creator never fires and is left out; a creator
    with no account yet fires as a plain member.
    """
    rows = await session.execute(
        select(Routine.created_by_user_id, Account.role, Account.platform_role_ids)
        .outerjoin(
            PlatformPrincipal,
            and_(
                PlatformPrincipal.tenant_id == Routine.tenant_id,
                PlatformPrincipal.platform == platform,
                PlatformPrincipal.external_id == Routine.created_by_user_id,
            ),
        )
        .outerjoin(Account, Account.id == PlatformPrincipal.account_id)
        .where(
            Routine.tenant_id == tenant_id,
            or_(Routine.agent_name.in_(agent_names), Routine.agent_id == agent_id),
            Routine.created_by_user_id.is_not(None),
        )
        .distinct()
    )
    return [
        UnattendedRequester(
            platform_user_id=user_id,
            is_admin=role == Role.ADMIN,
            role_ids=tuple(role_ids or ()),
        )
        for user_id, role, role_ids in rows.tuples()
        if user_id is not None
    ]


async def list_routine_channel_ids(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    agent_names: Collection[str],
    agent_id: str | None = None,
    caller_platform_user_id: str | None = None,
) -> list[str | None]:
    """The channels of the routines that run the agent, paused ones included.

    None stands for a routine with no channel. The caller's own routines are
    left out; None for `caller_platform_user_id` counts them all.
    """
    statement = select(Routine.channel_id).where(
        Routine.tenant_id == tenant_id,
        or_(Routine.agent_name.in_(agent_names), Routine.agent_id == agent_id),
    )
    if caller_platform_user_id is not None:
        statement = statement.where(
            Routine.created_by_user_id.is_distinct_from(caller_platform_user_id)
        )
    return list((await session.execute(statement.distinct())).scalars())


async def update_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    cron_expr: str | None = None,
    timezone_: str | None = None,
    trigger_message: str | None = None,
    enabled: bool | None = None,
    catch_up_policy: CatchUpPolicy | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    next_fire_at: datetime | None = None,
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    clear_destination: bool = False,
    channel_id: str | None = None,
) -> RoutineRow | None:
    """PATCH: `None` leaves a field alone. A destination is set as a pair, with
    the `channel_id` it resolves to; `clear_destination=True` removes the pair
    (and any pending delivery) but keeps `channel_id`, so the routine's spend
    still counts toward the same budget."""
    values: dict[str, str | bool | datetime | None] = {}
    if clear_destination:
        if destination_kind is not None or destination_id is not None:
            raise StoreError("clear_destination cannot be combined with a new destination")
        values.update(
            destination_kind=None,
            destination_id=None,
            delivery_status=None,
            delivery_payload=None,
            delivery_note=None,
        )
    elif destination_kind is not None or destination_id is not None:
        _check_destination(destination_kind, destination_id)
        values.update(
            destination_kind=destination_kind,
            destination_id=destination_id,
            channel_id=channel_id,
        )
        current = await get_routine(session, routine_id, tenant_id=tenant_id)
        if (
            current is not None
            and current.delivery_status == "pending"
            and (current.destination_kind, current.destination_id)
            != (destination_kind, destination_id)
        ):
            # A result queued for the old destination is not posted to the
            # new one.
            values.update(
                delivery_status="skipped",
                delivery_note="destination_changed",
                delivery_payload=None,
            )
    if catch_up_policy is not None:
        if catch_up_policy not in ("skip", "run-once"):
            raise StoreError("catch_up_policy must be skip or run-once")
        values["catch_up_policy"] = catch_up_policy
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
        return await get_routine(session, routine_id, tenant_id=tenant_id)

    stmt = (
        update(Routine)
        .where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        .values(**values, updated_at=func.now())
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


async def delete_routine(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> bool:
    result = await session.execute(
        delete(Routine).where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
    )
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


async def pause_routine(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> RoutineRow | None:
    """Set ``enabled=False`` and ``next_fire_at=NULL`` atomically.

    Returns the updated row, or ``None`` if no row matched ``routine_id``
    (including a routine_id that exists under a different tenant_id).
    """
    stmt = (
        update(Routine)
        .where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        .values(enabled=False, next_fire_at=None, updated_at=func.now())
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
    tenant_id: _uuid.UUID,
    now: datetime,
) -> RoutineRow | None:
    """Set ``enabled=True`` and ``next_fire_at`` to the next cron slot at-or-after ``now``.

    Caller injects ``now`` so this helper stays clock-free. Returns the
    updated row, or ``None`` if no row matched ``routine_id``
    (including a routine_id that exists under a different tenant_id).
    """
    row = await get_routine(session, routine_id, tenant_id=tenant_id)
    if row is None:
        return None
    nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
    stmt = (
        update(Routine)
        .where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        .values(enabled=True, next_fire_at=nxt, updated_at=func.now())
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
    delivery: Literal["pending", "skipped"] | None = None,
    delivery_note: str | None = None,
) -> None:
    """Set `last_result_tail` and `last_error` in a single UPDATE.

    `error=None` clears `last_error` (sets NULL); `error="..."` sets it.
    `tail` is written as-is (None or str).

    `delivery` is only passed for a routine with a destination (FEAT-085):
    `pending` queues this fire's tail for the adapter to post (copied into
    `delivery_payload`), `skipped` records why it will not be
    (`delivery_note`). Either replaces whatever an earlier fire left in the
    outbox, so only the newest result is ever posted. Omitted, the outbox
    columns are untouched — a routine without a destination, a failed fire,
    and an older scheduler all write exactly what they wrote before, and a
    result still pending from an earlier successful fire stays pending.
    """
    values: dict[str, object] = {"last_result_tail": tail, "last_error": error}
    if delivery is not None:
        values.update(
            delivery_status=delivery,
            delivery_note=delivery_note,
            delivery_payload=tail if delivery == "pending" else None,
            delivery_lease_owner=None,
            delivery_lease_expires_at=None,
            delivered_at=None,
        )
    await session.execute(update(Routine).where(Routine.id == routine_id).values(**values))
    await session.flush()


def _check_destination(kind: str | None, destination_id: str | None) -> None:
    if (kind is None) != (destination_id is None):
        raise StoreError("destination_kind and destination_id are set together")
    if kind is not None and kind not in ("channel", "thread"):
        raise StoreError("destination_kind must be channel or thread")
    if destination_id is not None and not destination_id.strip():
        raise StoreError("destination_id must not be empty")


async def claim_routine_deliveries(
    session: AsyncSession,
    *,
    platform: str,
    owner: str,
    now: datetime,
    lease: timedelta,
    limit: int = 20,
) -> list[RoutineRow]:
    """Claim pending result posts for `platform`'s tenants, oldest first.

    At most once: a claim whose lease ran out is never handed out again — the
    owner may have posted before it died — it is settled `skipped` with note
    `interrupted` first. Pending rows are then claimed with `FOR UPDATE SKIP
    LOCKED`, so two adapter processes never take the same row.
    """
    # Archived tenants are left alone: their rows stay pending and post if
    # the workspace comes back.
    tenant_ids = select(Tenant.id).where(Tenant.platform == platform, Tenant.archived_at.is_(None))
    await session.execute(
        update(Routine)
        .where(
            Routine.delivery_status == "claimed",
            Routine.delivery_lease_expires_at < now,
            Routine.tenant_id.in_(tenant_ids),
        )
        .values(
            delivery_status="skipped",
            delivery_note="interrupted",
            delivery_lease_owner=None,
            delivery_lease_expires_at=None,
        )
    )
    due = (
        select(Routine.id)
        .where(Routine.delivery_status == "pending", Routine.tenant_id.in_(tenant_ids))
        .order_by(Routine.last_fired_at.asc().nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(Routine)
        .where(Routine.id.in_(due))
        .values(
            delivery_status="claimed",
            delivery_lease_owner=owner,
            delivery_lease_expires_at=now + lease,
        )
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    rows = [RoutineRow.model_validate(orm) for orm in result.scalars().all()]
    await session.flush()
    return rows


async def settle_routine_delivery(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    owner: str,
    status: Literal["delivered", "skipped"],
    now: datetime,
    note: str | None = None,
) -> bool:
    """Finish a claim `owner` still holds. False = the claim was lost."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Routine)
            .where(
                Routine.id == routine_id,
                Routine.delivery_status == "claimed",
                Routine.delivery_lease_owner == owner,
            )
            .values(
                delivery_status=status,
                delivery_note=note,
                delivered_at=now if status == "delivered" else None,
                delivery_lease_owner=None,
                delivery_lease_expires_at=None,
            )
        ),
    )
    await session.flush()
    return result.rowcount == 1


async def set_last_fired_at(
    session: AsyncSession, routine_id: _uuid.UUID, *, last_fired_at: datetime
) -> None:
    """Directly set `last_fired_at` on one routine.

    Test/seed support for building a routine that has already fired at a known
    timestamp — the scheduler's normal path sets this atomically as part of
    `claim_due_fireable`'s batch claim, which isn't suitable for seeding a
    single row at an arbitrary instant.
    """
    await session.execute(
        update(Routine).where(Routine.id == routine_id).values(last_fired_at=last_fired_at)
    )
    await session.flush()


async def claim_due_fireable(
    session: AsyncSession,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
    limit: int = 20,
    exclude_ids: frozenset[_uuid.UUID] = frozenset(),
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
            or_(Routine.catch_up_policy == "run-once", Routine.next_fire_at >= window_start),
            Routine.id.not_in(exclude_ids),
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
    in_flight_versions: Mapping[_uuid.UUID, datetime] | None = None,
) -> int:
    """Roll forward skipped-policy stale slots, active-run slots, and NULL orphans.

    Run-once stale slots remain due for claiming. Record skipped ranges without
    overwriting the result of an active run. Returns count touched.
    """
    cutoff = now - max_age
    versions = in_flight_versions or {}
    in_flight_ids = frozenset(versions)
    unchanged_active = or_(
        false(),
        *(
            and_(Routine.id == key, Routine.updated_at == version)
            for key, version in versions.items()
        ),
    )
    stmt = (
        select(Routine)
        .where(
            Routine.enabled.is_(True),
            or_(
                and_(
                    Routine.id.not_in(in_flight_ids),
                    or_(
                        and_(Routine.catch_up_policy == "skip", Routine.next_fire_at < cutoff),
                        Routine.next_fire_at.is_(None),
                    ),
                ),
                and_(unchanged_active, Routine.next_fire_at <= now),
            ),
        )
        .order_by(
            Routine.id.in_(in_flight_ids).desc(),
            Routine.next_fire_at.asc().nullsfirst(),
            Routine.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows_orm = (await session.execute(stmt)).scalars().all()
    rows = [RoutineRow.model_validate(r) for r in rows_orm]

    touched = 0
    for row in rows:
        try:
            nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
            values: dict[str, datetime | str] = {"next_fire_at": nxt}
            if row.next_fire_at is not None:
                reason = "in_flight" if row.id in in_flight_ids else "stale"
                values.update(
                    last_skipped_from=row.next_fire_at,
                    last_skipped_until=now,
                    last_skip_reason=reason,
                )
                log.info(
                    "scheduler.slots_skipped",
                    routine_id=str(row.id),
                    skipped_from=row.next_fire_at.isoformat(),
                    skipped_until=now.isoformat(),
                    reason=reason,
                )
            await session.execute(update(Routine).where(Routine.id == row.id).values(**values))
            touched += 1
        except Exception:
            log.exception("advance_stale recompute failed", routine_id=str(row.id))

    await session.flush()
    return touched


async def skip_slots_during_fire(
    session: AsyncSession,
    *,
    routine_id: _uuid.UUID,
    finished_at: datetime,
    expected_updated_at: datetime,
) -> None:
    """Skip slots due before completion, unless the user edited this run's schedule."""
    orm = (
        await session.execute(
            select(Routine)
            .where(
                Routine.id == routine_id,
                Routine.enabled.is_(True),
                Routine.updated_at == expected_updated_at,
                Routine.next_fire_at <= finished_at,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if orm is None:
        return
    skipped_from = orm.next_fire_at
    nxt = next_slot_at_or_after(orm.cron_expr, orm.timezone, finished_at)
    await session.execute(
        update(Routine)
        .where(Routine.id == routine_id)
        .values(
            next_fire_at=nxt,
            last_skipped_from=skipped_from,
            last_skipped_until=finished_at,
            last_skip_reason="in_flight",
        )
    )
    log.info(
        "scheduler.slots_skipped",
        routine_id=str(routine_id),
        skipped_from=skipped_from,
        skipped_until=finished_at,
        reason="in_flight",
    )
