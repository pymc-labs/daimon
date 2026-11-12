"""A replacement session's framing, kept until its first message is sent (`turn.run`)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from anthropic.types.beta import BetaManagedAgentsSystemContentBlockParam
from daimon.core._models import PendingCarryOver
from daimon.core.stores.domain import PendingCarryOverRow
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def record_pending_carry_over(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: str,
    account_id: uuid.UUID | None,
    user_prefix: str,
    system_blocks: Sequence[BetaManagedAgentsSystemContentBlockParam],
) -> None:
    """Keep the framing for `session_id`; the first one written wins."""
    await session.execute(
        insert(PendingCarryOver)
        .values(
            tenant_id=tenant_id,
            session_id=session_id,
            account_id=account_id,
            user_prefix=user_prefix,
            system_blocks=[dict(block) for block in system_blocks],
        )
        .on_conflict_do_nothing(constraint="uq_pending_carry_overs_session")
    )


async def get_pending_carry_over(
    session: AsyncSession, *, tenant_id: uuid.UUID, session_id: str
) -> PendingCarryOverRow | None:
    """The framing `session_id` was never sent, or None."""
    row = await session.scalar(
        select(PendingCarryOver).where(
            PendingCarryOver.tenant_id == tenant_id, PendingCarryOver.session_id == session_id
        )
    )
    return PendingCarryOverRow.model_validate(row) if row is not None else None


async def delete_pending_carry_over(
    session: AsyncSession, *, tenant_id: uuid.UUID, session_id: str
) -> None:
    """Drop the framing once the session's first message is sent."""
    await session.execute(
        delete(PendingCarryOver).where(
            PendingCarryOver.tenant_id == tenant_id, PendingCarryOver.session_id == session_id
        )
    )


async def delete_stale_pending_carry_overs(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> int:
    """Delete at most `batch_size` rows written before `cutoff`."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    victims = (
        select(PendingCarryOver.id)
        .where(PendingCarryOver.created_at < cutoff)
        .order_by(PendingCarryOver.created_at, PendingCarryOver.id)
        .limit(batch_size)
    )
    result = await session.execute(delete(PendingCarryOver).where(PendingCarryOver.id.in_(victims)))
    return cast(CursorResult[object], result).rowcount or 0
