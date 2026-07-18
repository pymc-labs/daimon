"""thread_sessions store — newest-row-wins + keep-dead-rows model.

A Discord thread maps to ONE persisted MA session per caller. Multiple rows may
exist for the same (tenant_id, platform, thread_id, account_id) 4-tuple: the
live lookup returns the newest live row (ORDER BY created_at DESC LIMIT 1) that
belongs to the requesting caller's account. Dead rows are kept as audit so the
recreate-on-4xx path can insert a second row without losing the history of prior
sessions.

Security invariant: get_live_thread_session filters by account_id equality —
NULL rows (frozen pre-migration rows) never match any non-null caller, so every
existing thread cold-creates a fresh per-caller session on the next turn.

No unique constraint on thread identity — recreate intentionally inserts a
second row while marking the old row 'dead'.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from daimon.core._models import ThreadSession
from daimon.core.stores.domain import ThreadSessionRow
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def get_live_thread_session(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    account_id: _uuid.UUID,
) -> ThreadSessionRow | None:
    """Return the newest live row for (tenant_id, platform, thread_id, account_id), or None.

    The account_id filter is a plain equality predicate — no OR IS NULL branch.
    Rows with account_id NULL (pre-migration frozen rows) never match any live
    caller, so those threads cold-create a fresh per-caller session on the next
    turn (security guard).
    """
    orm = (
        await session.execute(
            select(ThreadSession)
            .where(
                ThreadSession.tenant_id == tenant_id,
                ThreadSession.platform == platform,
                ThreadSession.thread_id == thread_id,
                ThreadSession.account_id == account_id,
                ThreadSession.status == "live",
            )
            .order_by(ThreadSession.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if orm is None:
        return None
    return ThreadSessionRow.model_validate(orm)


async def create_thread_session(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    account_id: _uuid.UUID,
    ma_session_id: str,
    watermark_message_id: str | None = None,
    created_at: datetime | None = None,
) -> ThreadSessionRow:
    """Insert a new thread-session mapping row and return the Pydantic domain type.

    account_id is the calling user's account and is persisted on the row so that
    get_live_thread_session can scope future lookups to the same caller.

    The optional `created_at` kwarg is provided so tests can control ordering
    deterministically for newest-row-wins assertions. When None, the DB
    server_default (now()) applies.
    """
    kwargs: dict[str, object] = {
        "tenant_id": tenant_id,
        "platform": platform,
        "thread_id": thread_id,
        "account_id": account_id,
        "ma_session_id": ma_session_id,
        "watermark_message_id": watermark_message_id,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    orm = ThreadSession(**kwargs)
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return ThreadSessionRow.model_validate(orm)


async def update_watermark(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    watermark_message_id: str,
) -> None:
    """Persist the bot's final reply message id as the watermark for this mapping."""
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(watermark_message_id=watermark_message_id)
    )
    await session.flush()


async def mark_dead(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
) -> None:
    """Mark a mapping row as dead (session expired or recreated).

    The row is retained in the table as an audit trail; it will be excluded
    from future get_live_thread_session lookups.
    """
    await session.execute(update(ThreadSession).where(ThreadSession.id == id).values(status="dead"))
    await session.flush()
