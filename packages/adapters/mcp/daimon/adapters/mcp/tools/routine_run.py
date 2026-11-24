"""Run a routine now, ahead of its schedule; a routine run reports what kept it from its task."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.capability_gate import chat_agent_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.routines import (
    _caller,  # pyright: ignore[reportPrivateUsage]
    _env,  # pyright: ignore[reportPrivateUsage]
    _tool_errors,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import routine_service
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routines import has_private_output
from daimon.core.stores.lead_intake import execution_is_active, workflow_execution_is_active
from daimon.core.stores.routines import get_routine
from daimon.core.workflows.fire_store import bound_step
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

RoutineProblem = Literal[
    "blocked_access", "missing_input", "source_unavailable", "partial_result", "other"
]
_NOT_A_RUN = "Only a running routine can report a routine problem."


@dataclass(frozen=True)
class RoutineRunQueued:
    routine_id: str
    next_fire_at: datetime | None
    """When the scheduler picks it up: the next tick."""


async def _run_routine_impl(
    runtime: McpRuntime, auth: AuthIdentity, routine_id: uuid.UUID
) -> RoutineRunQueued:
    with _tool_errors():
        row = await routine_service.run_now(_env(runtime, auth), _caller(auth), routine_id)
    return RoutineRunQueued(routine_id=str(row.id), next_fire_at=row.next_fire_at)


@dataclass(frozen=True)
class RoutineProblemReported:
    routine_id: str | None
    workflow_id: str | None = None
    """Set instead of `routine_id` when a workflow's routine step reported."""


async def _report_routine_problem_impl(
    runtime: McpRuntime, auth: AuthIdentity, category: RoutineProblem, note: str
) -> RoutineProblemReported:
    # The routine comes only from the signed credential of a live fire, never an argument.
    routine_id, execution_id = auth.routine_id, auth.execution_id
    if auth.workflow_run_id is not None and execution_id is not None:
        return await _report_step_problem(
            runtime, auth, auth.workflow_run_id, execution_id, category, note
        )
    if routine_id is None or execution_id is None:
        raise ToolError(_NOT_A_RUN)
    async with runtime.session_factory() as session:
        live = await execution_is_active(session, auth.tenant_id, routine_id, execution_id)
        row = await get_routine(session, routine_id, tenant_id=auth.tenant_id) if live else None
    if row is None:
        raise ToolError(_NOT_A_RUN)
    report_ops_event(
        runtime.session_factory,
        OpsEvent(
            "routine.goal_unmet",
            auth.tenant_id,
            reason=category,
            routine_id=row.id,
            agent_id=row.agent_id,
            # Admins read the note; a routine whose output is private keeps it to itself.
            note=None if has_private_output(row) else note,
        ),
    )
    return RoutineProblemReported(routine_id=str(row.id))


async def _report_step_problem(
    runtime: McpRuntime,
    auth: AuthIdentity,
    run_id: uuid.UUID,
    grant: uuid.UUID,
    category: RoutineProblem,
    note: str,
) -> RoutineProblemReported:
    """`report_routine_problem` from a workflow's routine step: its run's live grant only."""
    agent = await chat_agent_id(runtime, auth)
    async with runtime.session_factory() as session:
        live = await workflow_execution_is_active(session, auth.tenant_id, run_id, grant)
        step = (
            await bound_step(session, tenant_id=auth.tenant_id, run_id=run_id, agent_id=agent or "")
            if live
            else None
        )
    if step is None:
        raise ToolError(_NOT_A_RUN)
    report_ops_event(
        runtime.session_factory,
        OpsEvent(
            "routine.goal_unmet",
            auth.tenant_id,
            reason=category,
            agent_id=agent,
            note=None if has_private_output(step.subject) else note,
            workflow_id=step.workflow.id,
            workflow_run_id=run_id,
        ),
    )
    return RoutineProblemReported(routine_id=None, workflow_id=str(step.workflow.id))


def register_routine_run_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def run_routine(ctx: Context, routine_id: uuid.UUID) -> RoutineRunQueued:  # pyright: ignore[reportUnusedFunction]
        """Run a routine now, ahead of its schedule; its own schedule continues after.

        Legacy: prefer the workflow tools (create_workflow, run_workflow).

        The routine's owner or an admin. A paused routine is refused: resume it first.
        """
        return await _run_routine_impl(runtime, await _auth(ctx), routine_id)

    @mcp.tool
    async def report_routine_problem(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, category: RoutineProblem, note: str
    ) -> RoutineProblemReported:
        """Tell the admins this routine run could not do its task, and why.

        Legacy: prefer the workflow tools (create_workflow, run_workflow).

        Only inside a routine run, once, when something kept you from the task. `note` is
        one short line on what blocked you (at most 300 characters), with no private
        content; admins see it on their issues page. Still put the blocker in your reply.
        """
        return await _report_routine_problem_impl(runtime, await _auth(ctx), category, note)


__all__ = ["register_routine_run_tool"]
