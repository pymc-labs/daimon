"""Run a routine now, ahead of its schedule."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.routines import (
    _caller,  # pyright: ignore[reportPrivateUsage]
    _env,  # pyright: ignore[reportPrivateUsage]
    _tool_errors,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import routine_service
from fastmcp import Context, FastMCP


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


def register_routine_run_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def run_routine(ctx: Context, routine_id: uuid.UUID) -> RoutineRunQueued:  # pyright: ignore[reportUnusedFunction]
        """Run a routine now, ahead of its schedule; its own schedule continues after.

        The routine's owner or an admin. A paused routine is refused: resume it first.
        """
        return await _run_routine_impl(runtime, await _auth(ctx), routine_id)


__all__ = ["register_routine_run_tool"]
