"""Routine conversion: the cursor moves both ways, so no slot fires twice or goes missing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from daimon.core._models import (
    Routine,
    Tenant,
    Workflow,
    WorkflowRun,
    WorkflowTrigger,
    WorkflowTriggerEvent,
)
from daimon.core.cron import next_slot_at_or_after
from daimon.core.errors import RoutineMoved, StoreError
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.routine_move import RoutineSeedEntry, routine_rollback_document
from daimon.core.stores import routines, workflows
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores.routine_move import (
    moved_routines,
    pause_for_handback,
    rollback_rows,
    seed_routines,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.approvals import unattended_decider
from daimon.core.turn.posture import PolicyApproval
from daimon.core.workflows import convert, triggers
from daimon.core.workflows.access import Who
from daimon.core.workflows.agent_step import step_decider, step_policy
from daimon.core.workflows.catalog import AgentLister
from daimon.core.workflows.lanes import OWNER_DM
from daimon.core.workflows.preflight import missing_grant
from daimon.core.workflows.service import Env, Forbidden, Refused
from daimon.core.workflows.spec import AgentNode, parse_spec
from daimon.core.workflows.unattended import exposed_at_run
from daimon.core.workflows.validate import DEFAULT_RUN_BUDGET_USD
from daimon.testing.builders import new_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, agents_with, make_settings
from sqlalchemy import select, update
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


async def test_a_moved_routine_keeps_its_agents_tools_on_its_version(
    db_session: AsyncSession,
) -> None:
    """The run-time exposure check reads the tools stored on the version, so the move must
    record them as every other publish does."""
    env = await _env(db_session, agents=agents_with({"daimon": ["gmail/send_message"]}))
    flow = await convert.from_routine(env, await _routine(db_session, env))
    async with env.sm() as s:
        version = await workflows.get_version(
            s, tenant_id=env.who.tenant_id, workflow_id=flow, number=1
        )
    assert version is not None, "the move publishes v1"
    assert version.agent_tools == {"daimon": ["gmail/send_message"]}, version.agent_tools


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
    trigger = await db_session.scalar(select(WorkflowTrigger))
    assert trigger is not None and trigger.from_routine_id == routine_id, "chain kept"
    assert await convert.to_routine(env, flow) == routine_id
    async with env.sm() as s:
        back = await routines.get_routine(s, routine_id, tenant_id=env.who.tenant_id)
    assert back is not None and back.source_routine_id == source, "link untouched both ways"


async def test_rollback_export_follows_the_workflows_cursor_and_waits_for_it(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    source = uuid.uuid4()
    routine_id = await _routine(db_session, env, source_routine_id=source)
    flow = await convert.from_routine(env, routine_id)
    fired = SLOT + timedelta(seconds=5)
    assert await triggers.fire_schedules(env.sm, now=fired) == 1
    later = SLOT + timedelta(minutes=30)
    doc = await _export(env, at=later)
    entry = doc["routines"][0]
    assert entry["resume_after"] == SLOT.isoformat(), "the workflow's last slot, not the routine's"
    assert entry["enabled"] is True, "the workflow is active"
    assert not doc["final"], "not final while the workflow is active"
    assert any("runs as workflow" in r for r in doc["not_final_reasons"]), doc
    assert any("run in flight" in r for r in doc["not_final_reasons"]), "its slot waits to start"
    async with env.sm.begin() as s:
        await s.execute(update(Workflow).where(Workflow.id == flow).values(status="paused"))
        await s.execute(update(WorkflowTriggerEvent).values(status="started"))
        run = await runs_store.create_run(
            s,
            tenant_id=env.who.tenant_id,
            workflow_id=flow,
            version=1,
            mode="live",
            run_as_account_id=None,
            triggered_by={"kind": "schedule"},
            trigger_kind="schedule",
            trigger_key=convert.TRIGGER,
            inputs={},
            budget_usd=Decimal(1),
            deadline_at=None,
        )
    assert "run in flight" in " ".join((await _export(env, at=later))["not_final_reasons"])
    async with env.sm.begin() as s:
        await s.execute(
            update(WorkflowRun).where(WorkflowRun.id == run.id).values(status="succeeded")
        )
    paused = await _export(env, at=later)
    assert paused["final"], paused["not_final_reasons"]
    assert paused["routines"][0]["enabled"] is False
    assert paused["routines"][0]["resume_after"] == SLOT.isoformat(), "no slot replays"


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


@pytest.mark.parametrize("tools", [[], ["crm/search"]], ids=["channel", "tainted"])
async def test_converted_step_decides_like_its_routine(
    db_session: AsyncSession, tools: list[str]
) -> None:
    """`writes: routine` keeps the operator's routine policy: each call goes out, or not, as
    it did from the routine. A step reading outside content keeps doing so under a waiver. A DM
    routine's step runs as the owner's DM turn, the routine's own decider."""
    env = await _env(db_session, agents=agents_with({"daimon": tools}))
    routine_id = await _routine(db_session, env)
    plan = await convert.preview(env, routine_id)
    spec = parse_spec(plan.spec)
    node = spec.nodes[convert.STEP]
    assert isinstance(node, AgentNode) and node.writes == "routine"
    assert (node.untrusted_waiver is not None) == bool(tools), node.untrusted_waiver
    assert not plan.validation.errors, "the Rule of Two holds through the waiver"
    assert ("untrusted_waiver" in {g.kind for g in plan.validation.grants}) == bool(tools)
    policy = ToolSafetyPolicy(
        enabled=True, unattended_writes=("crm/create_deal",), denied=("crm/delete_deal",)
    )
    trusted = frozenset({DAIMON_SERVER_NAME})
    routine = unattended_decider(policy, trusted_servers=trusted)
    exposed = exposed_at_run(spec, convert.STEP, untrusted_inputs=False, catalog=plan.catalog)
    step = step_decider(
        node,
        attempt_id=uuid.uuid4(),
        policy=policy,
        writes="routine",
        propose=None,
        exposed=exposed,
    )(
        PolicyApproval(
            decide=unattended_decider(step_policy(policy, "routine"), trusted_servers=trusted)
        )
    )

    def daimon(tool: str) -> ToolCall:
        return ToolCall(
            tool_use_id="t",
            server_name=DAIMON_SERVER_NAME,
            tool_name="call_tool",
            input={"name": tool, "arguments": {}},
        )

    calls = [
        *(
            ToolCall(tool_use_id="t", server_name="crm", tool_name=t)
            for t in ("search", "create_deal", "update_deal", "delete_deal")
        ),
        daimon("send_message"),
        daimon("list_channels"),
        ToolCall(tool_use_id="t", server_name=None, tool_name="bash"),
    ]
    for call in calls:
        want = (await routine(call)).allow
        assert (await step.decide(call)).allow is want, f"{call.server_name}/{call.tool_name}"
    flow = await convert.from_routine(env, routine_id)
    async with env.sm() as s:
        workflow = await workflows.get_workflow(s, tenant_id=env.who.tenant_id, workflow_id=flow)
        assert workflow is not None
        held = await missing_grant(s, workflow, 1, spec, DEFAULT_RUN_BUDGET_USD, policy)
    assert held is None, "the move records the grants the admin approved"
