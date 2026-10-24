"""Rows for `daimon.core.fleet_plan`: a plan is made pending, accepted once, then read
by every spawn of its thread's fleet.

A plan row keeps the plan and the caps it was built under, so a person's changes are
checked against the same limits the defaults came from.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from daimon.core._models import FleetPlan as FleetPlanRecord
from daimon.core.fleet_plan import Plan, PlanCaps, plan_digest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


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
            plan={"plan": plan.model_dump(mode="json"), "caps": caps.model_dump(mode="json")},
            digest=plan_digest(plan),
            expires_at=now + ttl,
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
    if row is None:
        return None
    return Plan.model_validate(row.plan["plan"]), PlanCaps.model_validate(row.plan["caps"])


async def accept_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    plan: Plan,
    caps: PlanCaps,
    now: datetime,
) -> bool:
    """Pending -> accepted, exactly once (a double tap accepts one plan); True when this
    call accepted it. `plan` is what the person accepted (the defaults or their edit)."""
    accepted = await session.scalar(
        update(FleetPlanRecord)
        .where(
            FleetPlanRecord.id == plan_id,
            FleetPlanRecord.tenant_id == tenant_id,
            FleetPlanRecord.account_id == account_id,
            FleetPlanRecord.status == "pending",
            FleetPlanRecord.expires_at > now,
        )
        .values(
            status="accepted",
            plan={"plan": plan.model_dump(mode="json"), "caps": caps.model_dump(mode="json")},
            digest=plan_digest(plan),
            accepted_at=now,
        )
        .returning(FleetPlanRecord.id)
    )
    return accepted is not None


async def accepted_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
) -> Plan | None:
    """The accepted plan for this person's thread, or None (absent, pending, or made in
    another conversation)."""
    row = await session.scalar(
        select(FleetPlanRecord).where(
            FleetPlanRecord.id == plan_id,
            FleetPlanRecord.tenant_id == tenant_id,
            FleetPlanRecord.account_id == account_id,
            FleetPlanRecord.status == "accepted",
            FleetPlanRecord.platform == platform,
            FleetPlanRecord.channel_id == channel_id,
            FleetPlanRecord.thread_id.is_(None)
            if thread_id is None
            else FleetPlanRecord.thread_id == thread_id,
        )
    )
    if row is None:
        return None
    plan = Plan.model_validate(row.plan["plan"])
    # The digest pins what was accepted: a row edited since is not that plan.
    return plan if plan_digest(plan) == row.digest else None
