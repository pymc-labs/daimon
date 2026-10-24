"""Reads and writes for the per-scope mode tables the scope cascade resolves.

Thread participation and progress plans keep one table each with the same
columns, so both stores share these queries. Callers own the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import ProgressPlanScope, ThreadParticipationScope
from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope
from sqlalchemy import CursorResult, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

ScopeModeTable = type[ThreadParticipationScope] | type[ProgressPlanScope]

# The workspace row needs no id of its own: tenant_id already names it.
_WORKSPACE_SCOPE_ID = ""


def _scope_id(scope: CascadeScope, scope_id: str | None) -> str:
    if scope is CascadeScope.WORKSPACE:
        return _WORKSPACE_SCOPE_ID
    if scope_id is None:
        raise ValueError(f"{scope.value} scope needs a scope_id")
    return scope_id


async def get_scope_modes(
    session: AsyncSession,
    table: ScopeModeTable,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> CascadeModes:
    """One read for the whole cascade: the workspace row plus this channel's and thread's rows."""
    keys: list[tuple[str, str]] = [(CascadeScope.WORKSPACE.value, _WORKSPACE_SCOPE_ID)]
    if channel_id is not None:
        keys.append((CascadeScope.CHANNEL.value, channel_id))
    if thread_id is not None:
        keys.append((CascadeScope.THREAD.value, thread_id))
    rows = (
        await session.execute(
            select(table.scope, table.mode).where(
                table.tenant_id == tenant_id,
                table.platform == platform,
                or_(*(tuple_(table.scope, table.scope_id) == k for k in keys)),
            )
        )
    ).all()
    found = {scope: CascadeMode(mode) for scope, mode in rows}
    return CascadeModes(
        workspace=found.get(CascadeScope.WORKSPACE.value),
        channel=found.get(CascadeScope.CHANNEL.value),
        thread=found.get(CascadeScope.THREAD.value),
    )


async def set_scope_mode(
    session: AsyncSession,
    table: ScopeModeTable,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
    mode: CascadeMode,
) -> None:
    stmt = (
        pg_insert(table)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            scope=scope.value,
            scope_id=_scope_id(scope, scope_id),
            mode=mode.value,
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            constraint=f"pk_{table.__tablename__}",
            set_={"mode": mode.value, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def clear_scope_mode(
    session: AsyncSession,
    table: ScopeModeTable,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
) -> bool:
    """Drop the explicit setting so the scope inherits again. Returns whether a row existed."""
    result = await session.execute(
        delete(table).where(
            table.tenant_id == tenant_id,
            table.platform == platform,
            table.scope == scope.value,
            table.scope_id == _scope_id(scope, scope_id),
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount == 1
