"""Rows of `thread_provider_release`: one pending provider release per caller slot.

The logic that records and applies them lives in `daimon.core.thread_release`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ThreadProviderRelease
from mux.state.lease import Slot
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


def _where(slot: Slot) -> tuple[ColumnElement[bool], ...]:
    assert slot.account_id is not None
    return (
        ThreadProviderRelease.tenant_id == uuid.UUID(slot.thread.channel.tenant_id),
        ThreadProviderRelease.platform == slot.thread.channel.platform,
        ThreadProviderRelease.channel_id == slot.thread.channel.channel_id,
        ThreadProviderRelease.thread_id == slot.thread.thread_id,
        ThreadProviderRelease.account_id == slot.account_id,
    )


async def put_request(session: AsyncSession, slot: Slot, *, generation: int, now: datetime) -> None:
    """Record or replace `slot`'s request, bound to the binding `generation` seen."""
    assert slot.account_id is not None
    await session.execute(
        insert(ThreadProviderRelease)
        .values(
            tenant_id=uuid.UUID(slot.thread.channel.tenant_id),
            platform=slot.thread.channel.platform,
            channel_id=slot.thread.channel.channel_id,
            thread_id=slot.thread.thread_id,
            account_id=slot.account_id,
            generation=generation,
            requested_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_thread_provider_release_slot",
            set_={"generation": generation, "requested_at": now},
        )
    )


async def take_request(session: AsyncSession, slot: Slot) -> int | None:
    """Lock, delete and return `slot`'s requested generation; None when none is pending."""
    query = select(ThreadProviderRelease.generation).where(*_where(slot)).with_for_update()
    generation = (await session.execute(query)).scalar_one_or_none()
    if generation is not None:
        await session.execute(delete(ThreadProviderRelease).where(*_where(slot)))
    return generation


async def requested_generation(session: AsyncSession, slot: Slot) -> int | None:
    """`slot`'s pending requested generation, read only."""
    query = select(ThreadProviderRelease.generation).where(*_where(slot))
    return (await session.execute(query)).scalar_one_or_none()


async def list_requests(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[tuple[str, str, str, int]]:
    """The tenant's pending requests as (channel, thread, account, generation)."""
    rows = await session.execute(
        select(
            ThreadProviderRelease.channel_id,
            ThreadProviderRelease.thread_id,
            ThreadProviderRelease.account_id,
            ThreadProviderRelease.generation,
        ).where(ThreadProviderRelease.tenant_id == tenant_id)
    )
    return [(row[0], row[1], row[2], row[3]) for row in rows]
