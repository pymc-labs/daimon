"""The `update_plan` tool: a checklist of a longer task's steps on the progress card.

``register_progress_plan_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure.

``update_plan`` is registered on every chat session, whether or not plans are
on, so the tool list stays the same from turn to turn and a prompt cache holds.
The turn says when plans are on (``daimon.core.turn.progress_plans``); when they
are off the card ignores the call. The switch itself is the ``progress_plans``
setting, changed with ``change_setting``.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from daimon.adapters.mcp.runtime import McpRuntime
from fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

_MAX_STEPS: Final = 12

_PLAN_UPDATED = "Plan updated. This is not a reply: carry on with the task."


class PlanStep(BaseModel):
    """One step of the plan, as the person reads it on the card."""

    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=120, description="A few plain words.")
    status: Literal["pending", "in_progress", "completed"]


def _update_plan_impl(steps: list[PlanStep]) -> str:
    """The card reads the plan from the call itself; the tool only acknowledges it."""
    del steps
    return _PLAN_UPDATED


def register_progress_plan_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    del runtime

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def update_plan(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        steps: Annotated[list[PlanStep], Field(min_length=1, max_length=_MAX_STEPS)],
    ) -> str:
        """Show the task's plan as a checklist on the progress card.

        Use it only when this turn's progress_plans block says progress plans
        are on; otherwise nothing is shown. Send the whole list each time, in
        order, with at most one step in_progress. Updating the plan is not a
        reply: carry on with the task afterwards.
        """
        del ctx
        return _update_plan_impl(steps)
