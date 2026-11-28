"""The Workflow step executor: start the called workflow's run and wait on it.

The child starts through `triggers.start_called` (call trigger, depth limit) and links to
the step; the step then waits as `event` until `runs.sync_children` settles it from the
child's result. A step claimed again (a lost worker) adopts the child that is still going.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core.config import Settings
from daimon.core.stores import workflow_runs
from daimon.core.workflows import engine, triggers
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.runs import Parked, Sessions, StepWork
from daimon.core.workflows.spec import WorkflowNode


@dataclass(frozen=True)
class WorkflowStepExecutor:
    sm: Sessions
    settings: Settings
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork) -> engine.Outcome | Parked:
        node = work.node
        assert isinstance(node, WorkflowNode), "the workflow executor runs workflow nodes"
        async with self.sm() as s:
            going = await workflow_runs.latest_child(s, work.step.id)
        if going is not None and going.status in workflow_runs.ACTIVE_RUN_STATES:
            return Parked(going.id, going.number)
        try:
            workflow_id = uuid.UUID(node.workflow)
        except ValueError:
            return engine.Failed("fatal", "The called workflow is not a workflow id")
        try:
            inputs = render(dict(node.inputs or {}), work.context, now=self.clock())
        except ExpressionError as err:
            return engine.Failed("fatal", str(err))
        assert isinstance(inputs, dict)
        child = await triggers.start_called(
            self.sm,
            self.settings,
            parent=work.run,
            workflow_id=workflow_id,
            inputs=inputs,
            now=self.clock(),
            parent_step_id=work.step.id,
            version=node.version if isinstance(node.version, int) else None,
        )
        if isinstance(child, Refusal):
            return engine.Failed("fatal", child.message, reason=child.reason)
        return Parked(child.id, child.number)
