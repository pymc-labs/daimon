"""Atomic Teams activity admission and conservative turn status."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import TeamsActivityClaim
from sqlalchemy import CursorResult, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: str,
    activity_id: str,
    thread_id: str,
) -> bool:
    result = await session.execute(
        insert(TeamsActivityClaim)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            activity_id=activity_id,
            thread_id=thread_id,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "conversation_id", "activity_id"])
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def link_outcome(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    thread_id: str,
    activity_ids: tuple[str, ...],
    outcome_id: uuid.UUID,
) -> None:
    await session.execute(
        update(TeamsActivityClaim)
        .where(
            TeamsActivityClaim.tenant_id == tenant_id,
            TeamsActivityClaim.thread_id == thread_id,
            TeamsActivityClaim.activity_id.in_(activity_ids),
        )
        .values(outcome_id=outcome_id)
    )


async def finish_outcome(session: AsyncSession, *, outcome_id: uuid.UUID) -> None:
    await session.execute(
        update(TeamsActivityClaim)
        .where(TeamsActivityClaim.outcome_id == outcome_id)
        .values(finished_at=datetime.now(UTC))
    )


async def delete_old(session: AsyncSession, *, cutoff: datetime, limit: int = 500) -> int:
    keys = (
        await session.execute(
            select(
                TeamsActivityClaim.tenant_id,
                TeamsActivityClaim.conversation_id,
                TeamsActivityClaim.activity_id,
            )
            .where(TeamsActivityClaim.created_at < cutoff)
            .limit(limit)
        )
    ).all()
    if not keys:
        return 0
    result = await session.execute(
        delete(TeamsActivityClaim).where(
            tuple_(
                TeamsActivityClaim.tenant_id,
                TeamsActivityClaim.conversation_id,
                TeamsActivityClaim.activity_id,
            ).in_(keys)
        )
    )
    return cast(CursorResult[Any], result).rowcount
