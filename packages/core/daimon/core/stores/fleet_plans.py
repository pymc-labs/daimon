"""Rows for `daimon.core.fleet_plan`: a plan is made pending, accepted once, then spent
by its thread's fleet, one claimed spawn at a time, until it is used up or expires.

A plan row keeps the plan and the caps it was built under, so a person's changes are
checked against the same limits the defaults came from. Every spawn under it is
claimed by its operation key under a row lock: past the plan's workers or its total
budget, a spawn is refused; a retry of a claimed spawn is the same one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from daimon.core._models import FleetPlan as FleetPlanRecord
from daimon.core.fleet_plan import Plan, PlanCaps, PlanRefused, check_within, plan_digest
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


def _record(plan: Plan, caps: PlanCaps) -> dict[str, Any]:
    return {"plan": plan.model_dump(mode="json"), "caps": caps.model_dump(mode="json")}


def _parts(row: FleetPlanRecord) -> tuple[Plan, PlanCaps]:
    return Plan.model_validate(row.plan["plan"]), PlanCaps.model_validate(row.plan["caps"])


async def create_plan(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    plan: Plan,
    caps: PlanCaps,
    now: datetime,
    ttl: timedelta,
) -> uuid.UUID:
    plan_id = uuid.uuid4()
    session.add(
        FleetPlanRecord(
            id=plan_id,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            status="pending",
            plan=_record(plan, caps),
            digest=plan_digest(plan),
            expires_at=now + ttl,
            claimed={},
            spent_usd=Decimal(0),
        )
    )
    await session.flush()
    return plan_id


async def pending_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime,
) -> tuple[Plan, PlanCaps] | None:
    """This person's plan, still waiting for them and not expired; else None."""
    row = await session.scalar(
        select(FleetPlanRecord).where(
            FleetPlanRecord.id == plan_id,
            FleetPlanRecord.tenant_id == tenant_id,
            FleetPlanRecord.account_id == account_id,
            FleetPlanRecord.status == "pending",
            FleetPlanRecord.expires_at > now,
        )
    )
    return None if row is None else _parts(row)


async def accept_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    plan: Plan,
    now: datetime,
    ttl: timedelta,
) -> bool:
    """Pending -> accepted, exactly once (a double tap accepts one plan); True when this
    call accepted it. `plan` is what the person accepted (the defaults or their edit);
    it is checked again here against the STORED caps and workers, so a caller can't
    widen it. The accepted plan is usable for `ttl` from now."""
    row = await session.scalar(
        select(FleetPlanRecord)
        .where(
            FleetPlanRecord.id == plan_id,
            FleetPlanRecord.tenant_id == tenant_id,
            FleetPlanRecord.account_id == account_id,
            FleetPlanRecord.status == "pending",
            FleetPlanRecord.expires_at > now,
        )
        .with_for_update()
    )
    if row is None:
        return False
    stored, caps = _parts(row)
    if plan.workers != stored.workers:
        raise PlanRefused("A fleet plan's workers are fixed when it is made. Make a new plan.")
    check_within(plan, caps)
    row.status = "accepted"
    row.plan = _record(plan, caps)
    row.digest = plan_digest(plan)
    row.accepted_at = now
    row.expires_at = now + ttl
    await session.flush()
    return True


async def _usable(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    now: datetime,
    plan_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] = ("accepted",),
) -> FleetPlanRecord | None:
    clauses = [
        FleetPlanRecord.tenant_id == tenant_id,
        FleetPlanRecord.account_id == account_id,
        FleetPlanRecord.status.in_(statuses),
        FleetPlanRecord.expires_at > now,
        FleetPlanRecord.platform == platform,
        FleetPlanRecord.channel_id == channel_id,
        FleetPlanRecord.thread_id.is_(None)
        if thread_id is None
        else FleetPlanRecord.thread_id == thread_id,
    ]
    if plan_id is not None:
        clauses.append(FleetPlanRecord.id == plan_id)
    if run_id is not None:
        clauses.append(FleetPlanRecord.run_id == run_id)
    row = await session.scalar(select(FleetPlanRecord).where(*clauses).with_for_update())
    if row is None:
        return None
    # The digest pins what was accepted: a row edited since is not that plan.
    return row if plan_digest(_parts(row)[0]) == row.digest else None


async def accepted_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    now: datetime,
) -> Plan | None:
    """The accepted, unexpired plan for this person's thread, or None (absent, pending,
    expired, or made in another conversation). A used-up plan is still returned: a retry
    of one of its spawns is that spawn, and `claim_spawn` refuses any new one."""
    row = await _usable(
        session,
        plan_id=plan_id,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        now=now,
        statuses=("accepted", "used"),
    )
    return None if row is None else _parts(row)[0]


async def claim_spawn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    now: datetime,
    operation_key: str,
    budget_usd: Decimal,
    worker: bool,
    plan_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Claim one spawn under the plan (by id, or a reviewer's by its run), under a row
    lock: a worker past the plan's workers, or any spawn past its total budget, is
    refused. A retry of a claimed operation key is the same spawn and claims nothing:
    False then (True for a new claim).
    The plan is marked used once all its workers are claimed (reviewers of that run
    may still spend what is left until it expires)."""
    row = await _usable(
        session,
        plan_id=plan_id,
        run_id=run_id,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        now=now,
        statuses=("accepted", "used"),
    )
    if row is None:
        raise PlanRefused(
            "That fleet plan isn't accepted for this conversation (it is pending, used "
            "up, expired, or another one's)."
        )
    plan = _parts(row)[0]
    claimed = dict(row.claimed)
    if operation_key in claimed:
        return False
    workers = sum(1 for entry in claimed.values() if entry.get("worker"))
    if worker and workers >= len(plan.workers):
        raise PlanRefused(
            f"All {len(plan.workers)} workers of the accepted fleet plan are started. "
            "Make a new plan for more."
        )
    total = plan.budget_usd * len(plan.workers)
    if row.spent_usd + budget_usd > total:
        raise PlanRefused(
            f"That goes past the accepted fleet plan's ${total} in total "
            f"(${row.spent_usd} is already set aside)."
        )
    claimed[operation_key] = {"budget_usd": str(budget_usd), "worker": worker}
    row.claimed = claimed
    row.spent_usd = row.spent_usd + budget_usd
    if worker and workers + 1 >= len(plan.workers):
        row.status = "used"
    await session.flush()
    return True


async def bind_run(
    session: AsyncSession, *, plan_id: uuid.UUID, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Record the run the plan's first spawn started (a reviewer of it spends from it)."""
    row = await session.scalar(
        select(FleetPlanRecord)
        .where(FleetPlanRecord.id == plan_id, FleetPlanRecord.tenant_id == tenant_id)
        .with_for_update()
    )
    if row is not None and row.run_id is None:
        row.run_id = run_id
        await session.flush()


async def release_spawn(
    session: AsyncSession, *, plan_id: uuid.UUID, tenant_id: uuid.UUID, operation_key: str
) -> None:
    """Give back a claim whose spawn failed before it started, so it can be tried again."""
    row = await session.scalar(
        select(FleetPlanRecord)
        .where(FleetPlanRecord.id == plan_id, FleetPlanRecord.tenant_id == tenant_id)
        .with_for_update()
    )
    if row is None or operation_key not in row.claimed:
        return
    claimed = dict(row.claimed)
    entry = claimed.pop(operation_key)
    row.claimed = claimed
    row.spent_usd = max(row.spent_usd - Decimal(entry["budget_usd"]), Decimal(0))
    if entry.get("worker") and row.status == "used":
        row.status = "accepted"
    await session.flush()


async def plan_id_for_run(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> uuid.UUID | None:
    return await session.scalar(
        select(FleetPlanRecord.id).where(
            FleetPlanRecord.tenant_id == tenant_id, FleetPlanRecord.run_id == run_id
        )
    )


#: How long an expired plan bound to a run is kept, so the run still reads as planned
#: (a late reviewer is told the plan expired, not that the run had none).
BOUND_PLAN_RETENTION = timedelta(days=7)


async def sweep_expired_plans(session: AsyncSession, *, now: datetime) -> int:
    """Delete plans past their expiry (pending, accepted or used: none can be spent).
    A plan bound to a run is kept `BOUND_PLAN_RETENTION` longer, for that binding."""
    result = await session.execute(
        delete(FleetPlanRecord).where(
            or_(
                and_(FleetPlanRecord.run_id.is_(None), FleetPlanRecord.expires_at <= now),
                FleetPlanRecord.expires_at <= now - BOUND_PLAN_RETENTION,
            )
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
