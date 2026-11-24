"""A routine step: one fire of the routine fire core, over the run's workflow state."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from daimon.core.fire.run import Dispatch, fire
from daimon.core.fire.subject import Subject, workflow_subject
from daimon.core.stores import identity, tenants, workflows
from daimon.core.stores.lead_intake import configured_workflows
from daimon.core.workflows import engine
from daimon.core.workflows.fire_store import WorkflowFireStore
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.spec import RoutineNode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

STALE = "The workflow changed while this ran."


@dataclass(frozen=True)
class RoutineStep:
    sm: async_sessionmaker[AsyncSession]
    dispatch: Dispatch
    #: A workspace agent's id by its spec key (`HeadlessLaneTurn.agent_id`).
    agent_id: Callable[[uuid.UUID, str | None], Awaitable[str]]
    #: The fire's turn (`fire_turn` with the scheduler's deps).
    turn: Callable[[Subject, WorkflowFireStore], Awaitable[None]]

    async def __call__(self, work: StepWork) -> engine.Outcome:
        node, run = work.node, work.run
        assert isinstance(node, RoutineNode), "a routine step runs a routine node"
        async with self.sm() as s:
            flow = await workflows.get_workflow(
                s, tenant_id=run.tenant_id, workflow_id=run.workflow_id
            )
            tenant = await tenants.get_tenant(s, run.tenant_id)
            owner = flow.owner_account_id if flow else None
            ids = (
                await identity.platform_external_ids(
                    s, tenant_id=run.tenant_id, platform=tenant.platform, account_ids=[owner]
                )
                if tenant and owner
                else {}
            )
            bound = (run.tenant_id, run.workflow_id) in await configured_workflows(
                s, {run.tenant_id}
            )
        if flow is None:
            return engine.Failed("fatal", STALE)
        agent_id = await self.agent_id(run.tenant_id, node.agent)
        subject = workflow_subject(
            flow,
            work.spec,
            node,
            run_id=run.id,
            owner=ids.get(owner) if owner else None,
            agent_id=agent_id,
        )
        store = WorkflowFireStore(self.sm, subject, flow, run, work.step.id)
        status = await fire(
            subject,
            store,
            lambda: self.turn(subject, store),
            self.dispatch,
            routines_gate=False,
            intake_bound=bound,
        )
        if status == "skipped":
            return engine.Succeeded({"skipped": "lead_intake_turned_off"})
        if status == "unreadable":
            return engine.Failed("retryable", "Could not read the features. Try again later.")
        done, private = store.outcome, subject.destination_kind == "dm"
        if done is None or done.status == "stale":
            return engine.Failed("fatal", STALE)
        if done.status == "failed":
            return engine.Failed(
                "fatal", "Private routine run failed." if private else done.error or "Failed"
            )
        delivery = str(done.delivery_id) if done.delivery_id else None
        return engine.Succeeded(
            {"delivery_id": delivery, "error": None}, summary="" if private else done.tail
        )
