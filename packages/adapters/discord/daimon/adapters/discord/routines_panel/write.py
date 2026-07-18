"""Adapter wrappers over core pause/resume helpers."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    pause_routine as _pause_core,
)
from daimon.core.stores.routines import (
    resume_routine as _resume_core,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def pause_routine_via_panel(
    session: AsyncSession, routine_id: uuid.UUID, *, tenant_id: uuid.UUID
) -> RoutineRow | None:
    """Adapter wrapper. Exists so panel imports never reach into core directly,
    keeping the routines_panel/* surface symmetrical with agent_setup/write.py."""
    return await _pause_core(session, routine_id, tenant_id=tenant_id)


async def resume_routine_via_panel(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
) -> RoutineRow | None:
    return await _resume_core(session, routine_id, tenant_id=tenant_id, now=now)
