"""Ephemeral Files-API object TTL queue. Phase 51 (D-07)."""

from __future__ import annotations

import datetime as dt

from daimon.core._models import PendingFileDelete
from daimon.core.stores.domain import PendingFileDeleteRow
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def enqueue_pending_file_delete(
    session: AsyncSession,
    *,
    file_id: str,
    delete_after: dt.datetime,
) -> None:
    """Enqueue a Files-API object for deletion. Idempotent on file_id (upsert)."""
    stmt = (
        pg_insert(PendingFileDelete)
        .values(file_id=file_id, delete_after=delete_after)
        .on_conflict_do_update(
            constraint="pk_pending_file_deletes",
            set_={"delete_after": delete_after},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def list_due_pending_file_deletes(
    session: AsyncSession,
    *,
    now: dt.datetime,
) -> list[PendingFileDeleteRow]:
    """Return rows whose delete_after <= now, ordered oldest first."""
    result = await session.execute(
        select(PendingFileDelete)
        .where(PendingFileDelete.delete_after <= now)
        .order_by(PendingFileDelete.delete_after)
    )
    return [PendingFileDeleteRow.model_validate(o) for o in result.scalars().all()]


async def delete_pending_file_delete(
    session: AsyncSession,
    *,
    file_id: str,
) -> None:
    """Remove the row by file_id. Idempotent — no raise if absent."""
    await session.execute(delete(PendingFileDelete).where(PendingFileDelete.file_id == file_id))
    await session.flush()
