"""One-shot timers: create, list and cancel.

A timer resumes the conversation it was set in, once, with the note the agent
left itself (`daimon.core.continuity.timers`). Creating one needs the active
turn's `origin_context_id`, which is what names the thread and the agent that
answers there; listing and cancelling are scoped to the caller's own timers in
their workspace (admins may cancel anyone's).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.continuity import timers
from daimon.core.continuity.timers import TimerError
from daimon.core.stores.domain import ChatPlatform, TaskContinuationRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field


class Timer(BaseModel):
    """One pending timer."""

    timer_id: str
    fire_at: datetime
    note: str
    thread_id: str
    agent_name: str


class TimerCancelResult(BaseModel):
    cancelled: bool
    timer_id: str


def _timer(row: TaskContinuationRow) -> Timer:
    assert row.available_at is not None and row.requested_work is not None
    return Timer(
        timer_id=str(row.idempotency_key),
        fire_at=row.available_at,
        note=row.requested_work,
        thread_id=row.thread_id,
        agent_name=row.target_name,
    )


async def _create_timer_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    fire_at: str,
    note: str,
) -> Timer:
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if auth.platform_user_id is None:
        raise ToolError(
            "A timer runs as the person who asked for it, and this connection does not "
            "carry their platform identity. Ask from the conversation itself."
        )
    # require_turn_origin admits only a chat platform's own turn origin.
    platform = cast(ChatPlatform, origin.platform)
    try:
        when = timers.parse_fire_at(fire_at, now=datetime.now(UTC))
        timer_id = await timers.schedule_timer(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            requester_account_id=auth.account_id,
            requester_external_user_id=auth.platform_user_id,
            target_ma_agent_id=origin.responder_ma_agent_id,
            target_name=origin.responder_name,
            note=note,
            fire_at=when,
        )
    except TimerError as exc:
        raise ToolError(str(exc)) from exc
    return Timer(
        timer_id=str(timer_id),
        fire_at=when,
        note=note.strip(),
        thread_id=origin.thread_id,
        agent_name=origin.responder_name,
    )


async def _list_timers_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[Timer]:
    rows = await timers.list_timers(
        runtime.session_factory, tenant_id=auth.tenant_id, account_id=auth.account_id
    )
    return [_timer(row) for row in rows]


async def _cancel_timer_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, timer_id: str
) -> TimerCancelResult:
    try:
        key = uuid.UUID(timer_id)
    except ValueError as exc:
        raise ToolError("timer not found") from exc
    cancelled = await timers.cancel_timer(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        timer_id=key,
        account_id=auth.account_id,
        is_admin=auth.is_admin,
    )
    if not cancelled:
        raise ToolError("timer not found, already fired, or already cancelled")
    return TimerCancelResult(cancelled=True, timer_id=timer_id)


def register_timer_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_timer(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        fire_at: Annotated[
            str,
            Field(description="ISO 8601 with a UTC offset, e.g. 2026-09-29T09:00:00+02:00."),
        ],
        note: Annotated[
            str,
            Field(description="What to do when it fires, written for yourself, with the details."),
        ],
    ) -> Timer:
        """Come back to this conversation once, later: "remind me in two hours", "check back tomorrow at 9".

        At `fire_at` you get one new turn in this same thread, with its context
        and your `note` as the message, as the person who asked. It fires once;
        for anything that repeats ("every Monday"), use `create_routine`.

        `fire_at` needs an explicit offset; use the time tools to work it out
        from the person's timezone. One minute to 90 days ahead. `note` is all
        you get besides the thread, so name what to check and what to report.
        Tell the person when it will fire and how to cancel it.
        """  # noqa: E501
        return await _create_timer_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            fire_at=fire_at,
            note=note,
        )

    @mcp.tool
    async def list_timers(ctx: Context) -> list[Timer]:  # pyright: ignore[reportUnusedFunction]
        """List the caller's pending timers in this workspace, soonest first."""
        return await _list_timers_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def cancel_timer(ctx: Context, timer_id: str) -> TimerCancelResult:  # pyright: ignore[reportUnusedFunction]
        """Cancel a pending timer by id (from `create_timer` or `list_timers`). A cancelled timer never fires."""  # noqa: E501
        return await _cancel_timer_impl(runtime, await _auth(ctx), timer_id=timer_id)
