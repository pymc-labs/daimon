"""Asks from a running turn to its helpers, and their replies (`daimon.core.helpers`)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime

from daimon.core._models import HelperAsk
from daimon.core.stores.domain import HelperAskRow, HelperAskStatus
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

OPEN: tuple[HelperAskStatus, ...] = ("queued", "running")


async def create_ask(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin_id: uuid.UUID,
    helper_agent_id: str,
    helper_name: str,
    message: str,
    now: datetime,
) -> HelperAskRow:
    ask = HelperAsk(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        origin_id=origin_id,
        helper_agent_id=helper_agent_id,
        helper_name=helper_name,
        message=message,
        status="queued",
        created_at=now,
    )
    session.add(ask)
    await session.flush()
    return HelperAskRow.model_validate(ask)


async def count_asks(session: AsyncSession, *, origin_id: uuid.UUID) -> int:
    """Every ask this turn has made, finished or not."""
    total = await session.scalar(
        select(func.count()).select_from(HelperAsk).where(HelperAsk.origin_id == origin_id)
    )
    return total or 0


async def list_asks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin_id: uuid.UUID,
    ids: Collection[uuid.UUID] | None = None,
) -> list[HelperAskRow]:
    """This turn's asks, oldest first; only `ids` when given."""
    query = select(HelperAsk).where(
        HelperAsk.tenant_id == tenant_id, HelperAsk.origin_id == origin_id
    )
    if ids is not None:
        query = query.where(HelperAsk.id.in_(list(ids)))
    rows = await session.scalars(query.order_by(HelperAsk.created_at, HelperAsk.id))
    return [HelperAskRow.model_validate(row) for row in rows]


async def claim_queued(
    session: AsyncSession, *, origin_id: uuid.UUID, skip_helpers: Collection[str], limit: int
) -> list[HelperAskRow]:
    """Mark up to `limit` queued asks running, oldest first, one per helper.

    A helper not in `skip_helpers` (those already running an ask) gets its oldest
    queued ask; its later ones wait, so a helper's conversation stays in order.
    """
    if limit <= 0:
        return []
    queued = await session.scalars(
        select(HelperAsk)
        .where(HelperAsk.origin_id == origin_id, HelperAsk.status == "queued")
        .order_by(HelperAsk.created_at, HelperAsk.id)
        .with_for_update(skip_locked=True)
    )
    claimed: list[HelperAsk] = []
    busy = set(skip_helpers)
    for ask in queued:
        if len(claimed) == limit:
            break
        if ask.helper_agent_id in busy:
            continue
        busy.add(ask.helper_agent_id)
        claimed.append(ask)
    if claimed:
        await session.execute(
            update(HelperAsk)
            .where(HelperAsk.id.in_([ask.id for ask in claimed]))
            .values(status="running")
        )
    return [
        HelperAskRow.model_validate(ask).model_copy(update={"status": "running"})
        for ask in claimed
    ]


async def finish_ask(
    session: AsyncSession,
    *,
    ask_id: uuid.UUID,
    status: HelperAskStatus,
    reply: str | None,
    now: datetime,
) -> None:
    await session.execute(
        update(HelperAsk)
        .where(HelperAsk.id == ask_id, HelperAsk.status.in_(OPEN))
        .values(status=status, reply=reply, finished_at=now)
    )


async def stop_open_asks(session: AsyncSession, *, origin_id: uuid.UUID, now: datetime) -> None:
    """End every ask still open for this turn: the turn that asked is over."""
    await session.execute(
        update(HelperAsk)
        .where(HelperAsk.origin_id == origin_id, HelperAsk.status.in_(OPEN))
        .values(status="stopped", finished_at=now)
    )
