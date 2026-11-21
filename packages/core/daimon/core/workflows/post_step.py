"""The post step executor: queue the step's post, then wait for the delivery loop to send it.

The post goes through `delivery.queue_deliver`, as a Finish's does, so a test run posts to
its tester only. The step waits as `event` until `runs.sync_children` settles it from the
post: where it landed (`{status, channel, thread, message, link}`), or a failure naming why
it did not go. A step claimed again (a lost worker) waits on the post it already queued.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core.stores import workflow_deliveries, workflows
from daimon.core.workflows import delivery, engine
from daimon.core.workflows.expressions import ExpressionError
from daimon.core.workflows.runs import Posted, Sessions, StepWork
from daimon.core.workflows.spec import PostNode


@dataclass(frozen=True)
class PostStepExecutor:
    sm: Sessions
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork) -> engine.Outcome | Posted:
        node = work.node
        assert isinstance(node, PostNode), "the post executor runs post nodes"
        async with self.sm.begin() as session:
            queued = await workflow_deliveries.post_of_step(session, work.step.id)
            if queued is not None:
                return Posted(queued.id)
            workflow = await workflows.get_workflow(
                session, tenant_id=work.run.tenant_id, workflow_id=work.run.workflow_id
            )
            assert workflow is not None, "a run outlives no workflow"
            try:
                delivery_id, _ = await delivery.queue_deliver(
                    session,
                    run=work.run,
                    workflow=workflow,
                    spec=work.spec,
                    deliver=node,
                    context=work.context,
                    now=self.clock(),
                    kind="post",
                    step_id=work.step.id,
                )
            except ExpressionError as err:
                return engine.Failed("fatal", str(err))
        return Posted(delivery_id)
