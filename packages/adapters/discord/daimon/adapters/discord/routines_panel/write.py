"""Adapter wrappers over core routine writes."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine as _create_core
from daimon.core.stores.routines import delete_routine as _delete_core
from daimon.core.stores.routines import (
    pause_routine as _pause_core,
)
from daimon.core.stores.routines import (
    resume_routine as _resume_core,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def pause_routine_via_panel(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    expected_updated_at: datetime | None = None,
) -> RoutineRow | None:
    """Adapter wrapper. Exists so panel imports never reach into core directly,
    keeping the routines_panel/* surface symmetrical with agent_setup/write.py."""
    return await _pause_core(
        session, routine_id, tenant_id=tenant_id, expected_updated_at=expected_updated_at
    )


async def resume_routine_via_panel(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    expected_updated_at: datetime | None = None,
) -> RoutineRow | None:
    return await _resume_core(
        session, routine_id, tenant_id=tenant_id, now=now, expected_updated_at=expected_updated_at
    )


async def create_routine_via_panel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: str,
    agent_id: str,
    agent_name: str,
    cron_expr: str,
    timezone_: str,
    trigger_message: str,
    next_fire_at: datetime,
    channel_id: str,
    source_thread_id: str | None,
    source_channel_id: str,
    source_provenance_known: bool,
) -> RoutineRow:
    return await _create_core(
        session,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone_=timezone_,
        trigger_message=trigger_message,
        next_fire_at=next_fire_at,
        channel_id=channel_id,
        source_thread_id=source_thread_id,
        source_channel_id=source_channel_id,
        source_provenance_known=source_provenance_known,
    )


async def delete_routine_via_panel(
    session: AsyncSession, routine_id: uuid.UUID, *, tenant_id: uuid.UUID
) -> bool:
    return await _delete_core(session, routine_id, tenant_id=tenant_id)
