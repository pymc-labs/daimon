"""A routine step's fire writes go to workflow state, and deleting the workflow takes them."""

from __future__ import annotations

import uuid

from daimon.core._models import FeatureValue, LeadIntakeConfig, Workflow, WorkflowDelivery
from daimon.core.fire.store import FireResult
from daimon.core.fire.subject import Subject
from daimon.core.stores import workflows
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.lead_intake import workflow_execution, workflow_execution_is_active
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.turn.termination import TerminationReason
from daimon.core.workflows.fire_store import WorkflowFireStore
from daimon.testing.workflows import World, make_world
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DONE = {"f": {"kind": "finish"}}


async def _parts(world: World) -> tuple[Subject, WorkflowRow, RunRow, uuid.UUID]:
    run = await world.run()
    step = (await world.steps(run.id))["f"]
    async with world.sm() as s:
        flow = await workflows.get_workflow(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
    assert flow is not None
    subject = Subject(
        id=world.workflow_id,
        tenant_id=world.tenant_id,
        created_by_user_id="u1",
        agent_id="ag",
        agent_name="Helper",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="go",
        destination_kind="channel",
        destination_id="555",
    )
    return subject, flow, run, step.id


async def _deliveries(world: World) -> list[WorkflowDelivery]:
    async with world.sm() as s:
        return list(await s.scalars(select(WorkflowDelivery).order_by(WorkflowDelivery.created_at)))


async def test_fails_finishes_and_pauses_on_workflow_state(db_session: AsyncSession) -> None:
    world = await make_world(db_session, DONE, "f", home="555")
    subject, flow, run, step_id = await _parts(world)
    store = WorkflowFireStore(world.sm, subject, flow, run, step_id)
    async with world.sm.begin() as s:
        assert await store.fail(s, error="boom", reason=TerminationReason.UPSTREAM)
    notice, owner = await _deliveries(world)
    assert (notice.kind, notice.status, notice.note, notice.target["id"]) == (
        "result",
        "pending",
        "failure_notice",
        "555",
    ), "the first failure of a streak tells its channel"
    assert (owner.kind, owner.target, owner.payload["topic"]) == (
        "notice",
        {"to": "owner"},
        "routine_failed",
    ), "and its owner, in the same transaction"
    await store.finish(FireResult("tail", "hi", delivery="skipped", note="agent_posted"))
    assert store.outcome is not None and store.outcome.tail == "tail"
    result = (await _deliveries(world))[-1]
    assert (result.kind, result.status, result.payload["text"]) == ("result", "skipped", "hi")

    moved = run.model_copy(update={"version": 9})
    stale = WorkflowFireStore(world.sm, subject, flow, moved, step_id)
    async with world.sm.begin() as s:
        assert not await stale.fail(s, error="boom", reason=TerminationReason.UPSTREAM)
        assert not await stale.pause_for_lapsed_role(s, error="role")
    assert stale.outcome is not None and stale.outcome.status == "stale"
    assert len(await _deliveries(world)) == 3, "a fire of a version no longer live writes nothing"
    async with world.sm.begin() as s:
        assert await store.pause_for_lapsed_role(s, error="role")
        assert not await store.pause_for_lapsed_role(s, error="role"), "already paused"


async def test_delete_takes_the_workflow_state_and_unbinds_intake(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, DONE, "f")
    run = await world.run()
    wid = world.workflow_id
    async with world.sm.begin() as s:
        s.add(
            LeadIntakeConfig(
                tenant_id=world.tenant_id,
                config={"workflow_id": str(wid)},
                encrypted_credentials=b"x",
            )
        )
        await set_feature_value(
            s,
            tenant_id=world.tenant_id,
            target=FeatureTarget("workflow", workflow_id=wid),
            key="routine_result_header",
            value="off",
            set_by=None,
            surface="web",
        )
    async with workflow_execution(world.sm, world.tenant_id, run.id, lifetime_s=60) as grant:
        async with world.sm.begin() as s:
            assert await workflow_execution_is_active(s, world.tenant_id, run.id, grant)
            assert await workflows.delete_workflow(s, tenant_id=world.tenant_id, workflow_id=wid)
        async with world.sm() as s:
            assert not await workflow_execution_is_active(s, world.tenant_id, run.id, grant)
            assert await s.scalar(select(Workflow.id).where(Workflow.id == wid)) is None
            assert (
                await s.scalar(select(FeatureValue.id).where(FeatureValue.workflow_id == wid))
                is None
            )
            config = await s.scalar(select(LeadIntakeConfig.config))
    assert config == {}, "the intake binding is cleared"
