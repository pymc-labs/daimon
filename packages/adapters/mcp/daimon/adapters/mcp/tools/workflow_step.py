"""The tools a workflow step's agent finishes its step with (`daimon.core.workflows.step_tools`).

Each takes `step`, the id its brief names; only the run's run-as person, in its workspace,
can act on a running step, and the workflow decider holds a lane session to its own step.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.workflows import step_tools, waits
from daimon.core.workflows.step_tools import FailKind, Grant, StepRefused
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Act = Callable[[AsyncSession, Grant], Awaitable[str]]


async def _on_step(runtime: McpRuntime, auth: AuthIdentity, step: uuid.UUID, act: Act) -> str:
    try:
        async with runtime.session_factory.begin() as s:
            g = await step_tools.grant(
                s, attempt_id=step, account_id=auth.account_id, tenant_id=auth.tenant_id, lock=True
            )
            return await act(s, g)
    except StepRefused as err:
        raise ToolError(str(err)) from err


def _now() -> datetime:
    return datetime.now(UTC)


def register_workflow_step_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def complete_step(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        step: uuid.UUID,
        summary: Annotated[str, Field(min_length=1, max_length=4000)],
        output: dict[str, JsonValue] | None = None,
        route: str | None = None,
        reason: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> str:
        """Finish your workflow step with its output, a markdown summary and its route if any.

        Only in a workflow step, with the step id its brief gives. The output is checked against
        the step's contract; on errors fix them and call again. `reason` says why you chose the
        route. After it is recorded, end your turn.
        """
        return await _on_step(
            runtime,
            await _auth(ctx),
            step,
            lambda s, g: step_tools.complete(
                s, g, output=output, summary=summary, route=route, reason=reason, now=_now()
            ),
        )

    @mcp.tool
    async def fail_step(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        step: uuid.UUID,
        reason: Annotated[str, Field(min_length=1, max_length=1000)],
        kind: FailKind,
    ) -> str:
        """End your workflow step as failed, with why.

        `kind` is missing_tool, missing_data, blocked or other, so the owner sees why without
        reading the transcript. End your turn afterwards.
        """
        return await _on_step(
            runtime,
            await _auth(ctx),
            step,
            lambda s, g: step_tools.fail(s, g, reason=reason, kind=kind, now=_now()),
        )

    @mcp.tool
    async def finish_early(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        step: uuid.UUID,
        summary: Annotated[str, Field(min_length=1, max_length=4000)],
    ) -> str:
        """End the whole workflow run early as done, because there is nothing to do.

        Only when your step's brief allows it. The run succeeds with nothing to do and its
        other steps are skipped. End your turn afterwards.
        """
        return await _on_step(
            runtime,
            await _auth(ctx),
            step,
            lambda s, g: step_tools.finish_early(s, g, summary=summary, now=_now()),
        )

    @mcp.tool
    async def step_context(ctx: Context, step: uuid.UUID) -> str:  # pyright: ignore[reportUnusedFunction]
        """Read your workflow step's brief again, with the outputs of the steps before it."""

        async def read(s: AsyncSession, g: Grant) -> str:
            return await step_tools.context_text(s, g)

        return await _on_step(runtime, await _auth(ctx), step, read)

    @mcp.tool
    async def ask_human(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        step: uuid.UUID,
        question: Annotated[str, Field(min_length=1, max_length=2000)],
        options: list[str] | None = None,
    ) -> str:
        """Ask a person a question from your workflow step, then end your turn.

        Only when the step allows questions, one open at a time and at most 3 a step.
        The answer arrives as your next message; carry on with the step from there.
        """

        async def put(s: AsyncSession, g: Grant) -> str:
            return await waits.ask(s, g, question=question, options=options or [], now=_now())

        return await _on_step(runtime, await _auth(ctx), step, put)


__all__ = ["register_workflow_step_tools"]
