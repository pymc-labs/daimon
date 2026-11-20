"""Routine conversion: the cursor moves both ways, so no slot fires twice or goes missing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from daimon.core._models import Routine, Tenant, Workflow, WorkflowTrigger
from daimon.core.errors import RoutineMoved
from daimon.core.stores import routines
from daimon.core.workflows import convert, triggers
from daimon.core.workflows.access import Who
from daimon.core.workflows.lanes import OWNER_DM
from daimon.core.workflows.service import Env, Forbidden, Refused
from daimon.testing.builders import new_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, make_settings
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLOT = NOW + timedelta(hours=1)


async def _env(db: AsyncSession, *, admin: bool = True) -> Env:
    tenant = await make_tenant(db)
    account = await make_account(db, tenant=tenant)
    await db.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    sm = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    return Env(sm, make_settings(), Who(tenant.id, account.id, admin=admin), NOW)


async def _routine(db: AsyncSession, env: Env, **fields: Any) -> uuid.UUID:
    defaults: dict[str, Any] = {"next_fire_at": SLOT}
    row = await new_routine(
        db,
        env.who.tenant_id,
        created_by_user_id="owner-1",
        cron_expr="0 * * * *",
        channel_id="c1",
        **(defaults | fields),
    )
    await db.commit()
    return row.id


async def _claimed(env: Env, now: datetime) -> list[uuid.UUID]:
    async with env.sm.begin() as s:
        return [r.id for r in await routines.claim_due_fireable(s, now=now)]


async def test_a_routine_moves_to_a_workflow_and_back_with_each_slot_once(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    routine_id = await _routine(db_session, env)
    await db_session.execute(update(Routine).values(last_fired_slot=NOW))
    await db_session.commit()
    flow = await convert.from_routine(env, routine_id)
    trigger = await db_session.scalar(select(WorkflowTrigger))
    assert trigger is not None and trigger.from_routine_id == routine_id, "linked"
    assert (trigger.next_fire_at, trigger.last_fired_slot) == (SLOT, NOW), "the cursor moved"
    async with env.sm() as s:
        moved = await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id)
        assert moved is not None and moved.next_fire_at is None and not moved.enabled
        with pytest.raises(RoutineMoved):
            await routines.update_routine(s, routine_id, tenant_id=env.who.tenant_id, name="x")
    assert await _claimed(env, SLOT + timedelta(seconds=5)) == [], "the routine stopped"
    assert await triggers.fire_schedules(env.sm, now=SLOT + timedelta(seconds=5)) == 1
    assert await convert.to_routine(env, flow) == routine_id
    nxt = SLOT + timedelta(hours=1)
    async with env.sm() as s:
        back = await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id)
        assert back is not None and back.enabled, "resumed, as the workflow was active"
        assert (back.next_fire_at, back.last_fired_slot) == (nxt, SLOT), "the cursor came back"
        status = await s.scalar(select(Workflow.status).where(Workflow.id == flow))
        assert status == "paused", "the workflow stays, paused"
    assert await triggers.fire_schedules(env.sm, now=nxt + timedelta(seconds=5)) == 0
    assert await _claimed(env, nxt + timedelta(seconds=5)) == [routine_id], "the routine fires"


async def test_a_paused_routine_moves_to_a_paused_workflow(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    flow = await convert.from_routine(env, await _routine(db_session, env, enabled=False))
    status = await db_session.scalar(select(Workflow.status).where(Workflow.id == flow))
    assert status == "paused"


async def test_routines_that_cannot_move_are_refused(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    handed = await _routine(db_session, env, source_routine_id=uuid.uuid4())
    with pytest.raises(Refused):
        await convert.from_routine(env, handed)
    running = await _routine(db_session, env)
    assert running in await _claimed(env, SLOT + timedelta(seconds=5))
    with pytest.raises(Refused):
        await convert.from_routine(env, running)
    plain = await _routine(db_session, env, next_fire_at=SLOT + timedelta(days=1))
    await convert.from_routine(env, plain)
    with pytest.raises(Refused):
        await convert.from_routine(env, plain)
    member = Env(env.sm, env.settings, Who(env.who.tenant_id, env.who.account_id), NOW)
    with pytest.raises(Forbidden):
        await convert.from_routine(member, plain)


async def test_a_dm_routine_becomes_an_owner_dm_step_that_posts_to_the_owner(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    async with env.sm() as s:
        row = await routines.get_routine(
            s, await _routine(db_session, env), tenant_id=env.who.tenant_id
        )
    assert row is not None
    dm = row.model_copy(update={"destination_kind": "dm", "destination_id": "owner-1"})
    spec = convert.routine_spec(dm, builtins=False)
    assert spec["nodes"][convert.STEP]["session"] == OWNER_DM
    assert spec["nodes"][convert.DONE]["deliver"]["to"] == "owner"
