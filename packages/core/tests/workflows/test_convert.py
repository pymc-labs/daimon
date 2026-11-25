"""Routine conversion: the cursor moves both ways, so no slot fires twice or goes missing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from daimon.core._models import (
    Account,
    Routine,
    Tenant,
    Workflow,
    WorkflowTrigger,
    WorkflowTriggerEvent,
    WorkflowVersion,
)
from daimon.core.cron import next_slot_at_or_after
from daimon.core.errors import StoreError
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.routine_move import RoutineSeedEntry, routine_rollback_document
from daimon.core.stores import routines, workflows
from daimon.core.stores.routine_move import (
    moved_routines,
    pause_for_handback,
    rollback_rows,
    seed_routines,
)
from daimon.core.workflows import convert, service, triggers
from daimon.core.workflows.access import Who
from daimon.core.workflows.catalog import AgentLister
from daimon.core.workflows.routine_translation import STEP
from daimon.core.workflows.service import Env, Forbidden, Refused
from daimon.core.workflows.spec import RoutineNode, parse_spec
from daimon.testing.builders import new_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, agents_with, make_settings
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLOT = NOW + timedelta(hours=1)


async def _env(db: AsyncSession, *, admin: bool = True, agents: AgentLister | None = None) -> Env:
    agents = agents or agents_with({"daimon": []})
    tenant = await make_tenant(db)
    account = await make_account(db, tenant=tenant)
    await db.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    sm = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    return Env(sm, make_settings(), Who(tenant.id, account.id, admin=admin), NOW, agents)


async def _routine(db: AsyncSession, env: Env, **fields: Any) -> uuid.UUID:
    defaults: dict[str, Any] = {
        "next_fire_at": SLOT,
        "created_by_user_id": "owner-1",
        "channel_id": "c1",
    }
    row = await new_routine(db, env.who.tenant_id, cron_expr="0 * * * *", **(defaults | fields))
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
    origin = await db_session.scalar(select(Workflow.converted_from).where(Workflow.id == flow))
    assert origin is not None and origin["routine_id"] == str(routine_id), "linked"
    assert trigger is not None
    assert (trigger.next_fire_at, trigger.last_fired_slot) == (SLOT, NOW), "the cursor moved"
    async with env.sm() as s:
        assert await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id) is None
    assert await _claimed(env, SLOT + timedelta(seconds=5)) == [], "the routine stopped"
    assert await triggers.fire_schedules(env.sm, now=SLOT + timedelta(seconds=5)) == 1
    assert await convert.to_routine(env, flow) == routine_id
    nxt = SLOT + timedelta(hours=1)
    async with env.sm() as s:
        back = await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id)
        assert back is not None and back.enabled, "resumed, as the workflow was active"
        assert (back.next_fire_at, back.last_fired_slot) == (nxt, SLOT), "the cursor came back"
        assert await s.scalar(select(Workflow.id).where(Workflow.id == flow)) is None, "gone"
    assert await triggers.fire_schedules(env.sm, now=nxt + timedelta(seconds=5)) == 0
    assert await _claimed(env, nxt + timedelta(seconds=5)) == [routine_id], "the routine fires"


async def test_a_fire_is_stuck_after_the_schedulers_configured_dispatch_timeout(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await _env(db_session)
    routine_id = await _routine(db_session, env)
    await db_session.execute(update(Routine).values(last_fired_at=NOW - timedelta(minutes=55)))
    await db_session.commit()
    monkeypatch.setenv("DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S", "7200")
    with pytest.raises(Refused):
        await convert.from_routine(env, routine_id)
    monkeypatch.delenv("DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S")
    await convert.from_routine(env, routine_id)


async def test_a_paused_routine_moves_to_a_paused_workflow(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    flow = await convert.from_routine(env, await _routine(db_session, env, enabled=False))
    status = await db_session.scalar(select(Workflow.status).where(Workflow.id == flow))
    assert status == "paused"


async def test_routines_that_cannot_move_are_refused(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    handed = await _routine(db_session, env, source_routine_id=uuid.uuid4())
    await db_session.execute(
        update(Routine).where(Routine.id == handed).values(handback_paused_at=NOW)
    )
    await db_session.commit()
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
    dm = await _routine(
        db_session, env, created_by_user_id="1001", destination_kind="dm", destination_id="1001"
    )
    plan = await convert.preview(env, dm)
    assert plan.spec["nodes"][STEP]["to"] == {"kind": "dm"}, "the owner's DM"
    assert not plan.validation.errors


async def test_a_stale_panel_button_on_a_moved_routine_changes_nothing(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    routine_id = await _routine(db_session, env)
    await convert.from_routine(env, routine_id)

    async def allowed(_row: object) -> bool:
        return True

    async with env.sm.begin() as s:
        result = await apply_routine_action(
            s,
            routine_id,
            tenant_id=env.who.tenant_id,
            op="resume",
            allowed=allowed,
            load=routines.get_routine,
            pause=routines.pause_routine,
            resume=routines.resume_routine,
            now=lambda: NOW,
        )
    assert result == "moved", "the panel says it moved instead of failing"


async def test_another_workspace_is_never_told_a_routine_moved(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    routine_id = await _routine(db_session, env)
    await convert.from_routine(env, routine_id)

    async def allowed(_row: object) -> bool:
        return True

    async with env.sm.begin() as s:
        result = await apply_routine_action(
            s,
            routine_id,
            tenant_id=uuid.uuid4(),
            op="resume",
            allowed=allowed,
            load=routines.get_routine,
            pause=routines.pause_routine,
            resume=routines.resume_routine,
            now=lambda: NOW,
        )
    assert result == "gone"


async def _export(env: Env, *, at: datetime) -> dict[str, Any]:
    async with env.sm() as s:
        rows = await rollback_rows(s, tenant_id=env.who.tenant_id)
        moved = await moved_routines(s, rows)
    return routine_rollback_document(rows, exported_at=at, moved=moved)


async def test_a_cutover_copy_converts_and_keeps_its_source_link(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    source = uuid.uuid4()
    routine_id = await _routine(db_session, env, source_routine_id=source)
    flow = await convert.from_routine(env, routine_id)
    origin = await db_session.scalar(select(Workflow.converted_from).where(Workflow.id == flow))
    assert origin is not None and origin["source_routine_id"] == str(source), "chain kept"
    assert await convert.to_routine(env, flow) == routine_id
    async with env.sm() as s:
        back = await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id)
    assert back is not None and back.source_routine_id == source, "link untouched both ways"


async def test_rollback_export_waits_for_a_moved_routine_and_keeps_the_workflows_cursor(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    source = uuid.uuid4()
    routine_id = await _routine(db_session, env, source_routine_id=source)
    flow = await convert.from_routine(env, routine_id)
    assert await triggers.fire_schedules(env.sm, now=SLOT + timedelta(seconds=5)) == 1
    later = SLOT + timedelta(minutes=30)
    with pytest.raises(StoreError, match=f"to-routine {flow}"):
        await _export(env, at=later)
    async with env.sm.begin() as s:
        await s.execute(delete(WorkflowTriggerEvent))
    await convert.to_routine(env, flow)
    entry = (await _export(env, at=later))["routines"][0]
    assert entry["routine_id"] == str(routine_id) and entry["enabled"] is True
    assert entry["resume_after"] == SLOT.isoformat(), "the workflow's last slot, so none replays"


async def test_pause_for_handback_refuses_a_moved_routine_until_it_moves_back(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    source = uuid.uuid4()
    routine_id = await _routine(db_session, env, source_routine_id=source)
    flow = await convert.from_routine(env, routine_id)
    async with env.sm.begin() as s:
        with pytest.raises(StoreError, match=f"to-routine {flow}"):
            await pause_for_handback(
                s, tenant_id=env.who.tenant_id, source_routine_ids=[source], now=NOW, apply=True
            )
    await convert.to_routine(env, flow)
    async with env.sm.begin() as s:
        rows = await pause_for_handback(
            s, tenant_id=env.who.tenant_id, source_routine_ids=[source], now=NOW, apply=True
        )
    assert [r.id for r in rows] == [routine_id], "back as a routine, it pauses for handback"


async def test_seed_refuses_a_routine_that_runs_as_a_workflow(db_session: AsyncSession) -> None:
    env = await _env(db_session)
    source = uuid.uuid4()
    routine_id = await _routine(db_session, env, source_routine_id=source, enabled=False)
    await convert.from_routine(env, routine_id)
    done = NOW - timedelta(hours=2)
    entry = RoutineSeedEntry(
        source_routine_id=source,
        last_completed_slot=done.isoformat(),  # pyright: ignore[reportArgumentType]  # ISO wire form
        source_next_run=next_slot_at_or_after("0 * * * *", "UTC", done).isoformat(),  # pyright: ignore[reportArgumentType]  # ISO wire form
        cron_expr="0 * * * *",
        timezone="UTC",
    )
    async with env.sm.begin() as s:
        with pytest.raises(StoreError, match="runs as a workflow"):
            await seed_routines(s, [entry], tenant_id=env.who.tenant_id, now=NOW, apply=True)


async def test_converted_step_decides_like_its_routine(db_session: AsyncSession) -> None:
    """The step is a routine step: it runs the routine's own fire, under its tool policy, so
    the preview has nothing for the admin to approve."""
    env = await _env(db_session, agents=agents_with({"daimon": ["crm/search"]}))
    routine_id = await _routine(db_session, env, trigger_message="Sum up the deals")
    plan = await convert.preview(env, routine_id)
    node = parse_spec(plan.spec).nodes[STEP]
    assert isinstance(node, RoutineNode)
    assert (node.agent, node.prompt) == ("daimon", "Sum up the deals")
    assert not plan.validation.errors and not plan.validation.grants


@pytest.mark.parametrize(
    "change",
    [
        {"every_weeks": 2, "anchor": "2026-11-16"},
        {"cron": None, "at": (NOW + timedelta(days=9)).isoformat()},
    ],
    ids=["every_weeks", "at"],
)
async def test_a_workflow_on_a_multi_week_or_one_time_schedule_does_not_move_back(
    db_session: AsyncSession, change: dict[str, Any]
) -> None:
    env = await _env(db_session)
    flow = await convert.from_routine(env, await _routine(db_session, env))
    async with env.sm.begin() as s:
        workflow = await workflows.get_workflow(s, tenant_id=env.who.tenant_id, workflow_id=flow)
        assert workflow is not None and workflow.live_version is not None
        live = await workflows.get_version(
            s, tenant_id=env.who.tenant_id, workflow_id=flow, number=workflow.live_version
        )
        assert live is not None
        trigger = {k: v for k, v in (live.spec["triggers"][0] | change).items() if v is not None}
        await workflows.publish_version(
            s,
            tenant_id=env.who.tenant_id,
            workflow_id=flow,
            spec={**live.spec, "triggers": [trigger]},
            spec_hash="h",
            source="web",
            published_by=env.who.account_id,
            now=NOW,
        )
    with pytest.raises(Refused, match="more than a routine can"):
        await convert.to_routine(env, flow)


async def test_only_the_owner_or_an_admin_runs_a_moved_routine_by_hand(
    db_session: AsyncSession,
) -> None:
    """Its step runs as the owner, so an editor or a runner cannot start, rerun or retry it."""
    env = await _env(db_session)
    tid = env.who.tenant_id
    flow = await convert.from_routine(env, await _routine(db_session, env))
    owner = await db_session.scalar(select(Workflow.owner_account_id).where(Workflow.id == flow))
    editor, runner = Account(tenant_id=tid), Account(tenant_id=tid)
    db_session.add_all([editor, runner])
    await db_session.commit()
    async with env.sm.begin() as s:
        await workflows.update_settings(s, tenant_id=tid, workflow_id=flow, visibility="shared")
        for role, person in (("editor", editor), ("runner", runner)):
            await workflows.set_access(
                s,
                tenant_id=tid,
                workflow_id=flow,
                role=role,
                granted_by=owner,
                account_id=person.id,
            )
    assert owner is not None

    def as_(account: uuid.UUID) -> Env:
        return Env(env.sm, env.settings, Who(tid, account), NOW, env.agents)

    run = uuid.UUID((await service.start(as_(owner), flow, {})).id)  # the owner may
    refused = [
        service.start(as_(runner.id), flow, {}),
        service.start(as_(editor.id), flow, {}),
        service.rerun(as_(editor.id), run),
        service.redrive(as_(editor.id), run),
    ]
    for call in refused:
        with pytest.raises(Forbidden, match="Ask the owner or an admin to run it"):
            await call


@pytest.mark.parametrize(
    ("fields", "hidden"),
    [
        ({"channel_id": None}, True),
        ({"created_by_user_id": "1001", "destination_kind": "dm", "destination_id": "1001"}, True),
        ({}, False),
    ],
    ids=["personal", "dm", "channel"],
)
async def test_a_moved_personal_routines_last_result_is_its_owners_alone(
    db_session: AsyncSession, fields: dict[str, Any], hidden: bool
) -> None:
    """A DM routine, or one with no destination or channel (it mounts personal memory): the
    strip shows its last result to its owner, never to an admin."""
    env = await _env(db_session)
    routine_id = await _routine(db_session, env, **fields)
    await db_session.execute(
        update(Routine).values(last_fired_at=NOW, last_finished_at=NOW, last_result_tail="Notes")
    )
    await db_session.commit()
    flow = await convert.from_routine(env, routine_id)
    owner = await db_session.scalar(select(Workflow.owner_account_id).where(Workflow.id == flow))
    assert owner is not None and owner != env.who.account_id, "the admin is not the owner"
    as_owner = Env(env.sm, env.settings, Who(env.who.tenant_id, owner), NOW, env.agents)
    plain = {
        "schema": "daimon.workflow/v1",
        "name": "T",
        "start": "f",
        "nodes": {"f": {"kind": "finish"}},
    }
    for edited in (False, True):
        if edited:  # an editor republishes it as an ordinary workflow: the text stays as hidden
            await db_session.execute(update(WorkflowVersion).values(spec=plain))
            await db_session.commit()
        for reader, shown in ((as_owner, "Notes"), (env, "" if hidden else "Notes")):
            migration = (await service.get(reader, flow)).migration
            assert migration is not None, f"{reader.who}"
            assert migration["last"]["text"] == shown, f"{reader.who}, edited {edited}"
