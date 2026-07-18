"""Routines tools: create / list / get / update / delete.

``register_routines_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

Partition scope: all five tools operate within the tenant from the caller's
JWT claims. Cross-partition access raises ``ToolError("routine not found")``
— same message for unknown vs. forbidden IDs so existence is not leaked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.cron import next_slot_at_or_after
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.stores import routines as routines_store
from daimon.core.stores.domain import RoutineRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel


class DeleteResult(BaseModel):
    deleted: bool
    routine_id: str


def _require_platform_user_id(auth: AuthIdentity) -> str:
    """Return auth.platform_user_id or raise ToolError if missing.

    Required for create_routine because the scheduler later needs an external_id
    to build a principal for the fired turn (scheduler/main.py:139). CLI sessions
    have no platform_user_id and therefore cannot create schedulable routines.
    """
    if auth.platform_user_id is None:
        raise ToolError("creating a routine requires a platform user identity")
    return auth.platform_user_id


def _compute_next_fire_at(cron_expr: str, tz: str) -> datetime:
    """Validate cron + timezone and return the next fire datetime (UTC).

    This is the ONE legitimate ``except Exception`` site in this module — it
    sits at the MCP boundary and immediately re-raises as ToolError (per
    guideline:architecture named-boundary rule). Croniter raises a mix of
    ValueError / KeyError on bad expressions; catching all is intentional here.
    """
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as e:
        raise ToolError(f"unknown timezone: {tz!r}") from e
    try:
        return next_slot_at_or_after(cron_expr, tz, datetime.now(UTC))
    except Exception as e:  # noqa: BLE001  # croniter raises mixed exception types; named boundary
        raise ToolError(f"invalid cron expression: {cron_expr!r}") from e


async def _create_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    cron_expr: str,
    timezone: str,
    trigger_message: str,
    enabled: bool = True,
) -> RoutineRow:
    tenant_id = auth.tenant_id
    platform_user_id = _require_platform_user_id(auth)
    next_fire_at = _compute_next_fire_at(cron_expr, timezone)

    match = await find_agent_by_daimon_tag(
        runtime.client,
        tenant_id=tenant_id,
        name=agent_name,
    )
    if match is None:
        raise ToolError(f"no agent named {agent_name!r} found for this tenant")
    agent_id = match.id

    async with runtime.session_factory() as session, session.begin():
        return await routines_store.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=platform_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone_=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            next_fire_at=next_fire_at,
        )


async def _list_routines_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[RoutineRow]:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session:
        return await routines_store.list_routines_for_tenant(session, tenant_id=tenant_id)


async def _get_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> RoutineRow:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session:
        row = await routines_store.get_routine(session, routine_id)
    if row is None or row.tenant_id != tenant_id:
        raise ToolError("routine not found")
    return row


async def _update_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
    agent_name: str | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    trigger_message: str | None = None,
    enabled: bool | None = None,
) -> RoutineRow:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id)
        if row is None or row.tenant_id != tenant_id:
            raise ToolError("routine not found")

        # Recompute next_fire_at only when cron or timezone is being changed.
        next_fire_at: datetime | None = None
        if cron_expr is not None or timezone is not None:
            effective_cron = cron_expr if cron_expr is not None else row.cron_expr
            effective_tz = timezone if timezone is not None else row.timezone
            next_fire_at = _compute_next_fire_at(effective_cron, effective_tz)

        # rename support via daimon-tag resolution at tool boundary.
        # If a new agent_name is provided and differs from the current one, look up
        # the live MA agent id and persist both fields. Unknown name -> ToolError.
        new_agent_id: str | None = None
        if agent_name is not None and agent_name != row.agent_name:
            match = await find_agent_by_daimon_tag(
                runtime.client,
                tenant_id=tenant_id,
                name=agent_name,
            )
            if match is None:
                raise ToolError(f"no agent named {agent_name!r} found for this tenant")
            new_agent_id = match.id

        updated = await routines_store.update_routine(
            session,
            routine_id,
            cron_expr=cron_expr,
            timezone_=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            agent_id=new_agent_id,
            agent_name=agent_name if new_agent_id is not None else None,
            next_fire_at=next_fire_at,
        )
        if updated is None:
            raise ToolError("routine not found")
        return updated


async def _delete_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> DeleteResult:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id)
        if row is None or row.tenant_id != tenant_id:
            raise ToolError("routine not found")
        await routines_store.delete_routine(session, routine_id)
    return DeleteResult(deleted=True, routine_id=str(routine_id))


def register_routines_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_routine(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        cron_expr: str,
        timezone: str,
        trigger_message: str,
        enabled: bool = True,
    ) -> RoutineRow:
        """Create a routine in the caller's tenant partition.

        ``agent_name`` MUST be the exact daimon-side display name of an
        existing agent on this tenant (e.g. ``"daimon"``, ``"daimon-copy"``,
        ``"research-bot"``). It is NOT:

        - a free-text description of the routine
        - a Discord mention, tag fragment, or user handle
        - a routine label or trigger phrase
        - the name of a tool, MCP, or skill

        The tool resolves ``agent_name`` to a live MA agent id at the call
        boundary; an unknown name raises ``ToolError`` and the
        routine is not created.

        If you are the calling agent and do not know which agent to bind the
        routine to, DEFAULT to your own name (the one you were addressed as
        in this conversation). Do NOT guess from context — if ambiguous, ask
        the user which agent should own the routine.

        Example: a user says "daimon, create a routine that pings me every
        5 minutes with the message 'ping'". The correct call is
        ``create_routine(agent_name="daimon", cron_expr="*/5 * * * *",
        timezone="UTC", trigger_message="ping")``.
        """
        return await _create_routine_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
        )

    @mcp.tool
    async def list_routines(ctx: Context) -> list[RoutineRow]:  # pyright: ignore[reportUnusedFunction]
        """List all routines in the caller's tenant partition."""
        return await _list_routines_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def get_routine(ctx: Context, routine_id: UUID) -> RoutineRow:  # pyright: ignore[reportUnusedFunction]
        """Get a routine by id (tenant-scoped; raises if not found or cross-tenant)."""
        return await _get_routine_impl(runtime, await _auth(ctx), routine_id=routine_id)

    @mcp.tool
    async def update_routine(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        routine_id: UUID,
        agent_name: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        trigger_message: str | None = None,
        enabled: bool | None = None,
    ) -> RoutineRow:
        """PATCH-update a routine. Only provided fields are changed.

        ``agent_name`` reassigns the routine to a different daimon-tagged agent;
        the tool resolves it to a live MA agent id at the call boundary.
        """
        return await _update_routine_impl(
            runtime,
            await _auth(ctx),
            routine_id=routine_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
        )

    @mcp.tool
    async def delete_routine(ctx: Context, routine_id: UUID) -> DeleteResult:  # pyright: ignore[reportUnusedFunction]
        """Delete a routine (hard delete, tenant-scoped)."""
        return await _delete_routine_impl(runtime, await _auth(ctx), routine_id=routine_id)
