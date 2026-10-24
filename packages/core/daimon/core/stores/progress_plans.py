"""Async store for progress plans: the explicit mode at each scope.

Callers own the transaction; every write ends with `await session.flush()`.
"""

from __future__ import annotations

import uuid

from daimon.core._models import ProgressPlanScope
from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope
from daimon.core.stores._scope_modes import clear_scope_mode, get_scope_modes, set_scope_mode
from sqlalchemy.ext.asyncio import AsyncSession


async def get_progress_plan_modes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> CascadeModes:
    """One read for the whole cascade: the workspace row plus this channel's and thread's rows."""
    return await get_scope_modes(
        session,
        ProgressPlanScope,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
    )


async def set_progress_plan_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
    mode: CascadeMode,
) -> None:
    await set_scope_mode(
        session,
        ProgressPlanScope,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        mode=mode,
    )


async def clear_progress_plan_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: CascadeScope,
    scope_id: str | None,
) -> bool:
    """Drop the explicit setting so the scope inherits again. Returns whether a row existed."""
    return await clear_scope_mode(
        session,
        ProgressPlanScope,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
    )
