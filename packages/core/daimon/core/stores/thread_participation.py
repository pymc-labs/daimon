"""Async store for organic thread participation: scope modes and the auto-response ledger.

Callers own the transaction; every write ends with `await session.flush()`.
No try/except -- exceptions propagate to the adapter boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ThreadAutoResponse, ThreadParticipationScope
from daimon.core.scope_cascade import CascadeModes
from daimon.core.stores._scope_modes import clear_scope_mode, get_scope_modes, set_scope_mode
from daimon.core.stores.domain import ThreadAutoResponseRow
from daimon.core.thread_participation import ParticipationMode, ParticipationScope
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

ParticipationModes = CascadeModes


async def get_participation_modes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> ParticipationModes:
    """One read for the whole cascade: the workspace row plus this channel's and thread's rows."""
    return await get_scope_modes(
        session,
        ThreadParticipationScope,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
    )


async def set_participation_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: ParticipationScope,
    scope_id: str | None,
    mode: ParticipationMode,
) -> None:
    await set_scope_mode(
        session,
        ThreadParticipationScope,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        mode=mode,
    )


async def clear_participation_mode(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: ParticipationScope,
    scope_id: str | None,
) -> bool:
    """Drop the explicit setting so the scope inherits again. Returns whether a row existed."""
    return await clear_scope_mode(
        session,
        ThreadParticipationScope,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
    )


async def record_auto_response(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    message_id: str,
    created_at: datetime | None = None,
) -> ThreadAutoResponseRow:
    """Append one ledger row. `created_at` exists so tests can backdate; production omits it.

    Two constructor calls rather than a kwargs splat: omitting `created_at`
    entirely is what lets the column default apply, and both calls stay
    type-checked.
    """
    orm = (
        ThreadAutoResponse(
            tenant_id=tenant_id, platform=platform, thread_id=thread_id, message_id=message_id
        )
        if created_at is None
        else ThreadAutoResponse(
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            message_id=message_id,
            created_at=created_at,
        )
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return ThreadAutoResponseRow.model_validate(orm)


async def count_auto_responses_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    since: datetime,
) -> int:
    """Unprompted replies in this thread at or after `since`: the rows are the rate-limit ledger."""
    return (
        await session.execute(
            select(func.count())
            .select_from(ThreadAutoResponse)
            .where(
                ThreadAutoResponse.tenant_id == tenant_id,
                ThreadAutoResponse.platform == platform,
                ThreadAutoResponse.thread_id == thread_id,
                ThreadAutoResponse.created_at >= since,
            )
        )
    ).scalar_one()
