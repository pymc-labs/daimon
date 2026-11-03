"""Async store for the model a conversation's people chose (`thread_model_settings`).

Callers own the transaction; every write ends with `await session.flush()`.
No try/except -- exceptions propagate to the adapter boundary.
"""

from __future__ import annotations

import uuid

from daimon.core._models import ThreadModelSetting
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_thread_model(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> str | None:
    """The model id chosen for this conversation, or None for the agent's own."""
    return await session.scalar(
        select(ThreadModelSetting.model).where(
            ThreadModelSetting.tenant_id == tenant_id,
            ThreadModelSetting.platform == platform,
            ThreadModelSetting.thread_id == thread_id,
        )
    )


async def set_thread_model(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    model: str,
) -> None:
    stmt = (
        pg_insert(ThreadModelSetting)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            model=model,
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            constraint="pk_thread_model_settings",
            set_={"model": model, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def clear_thread_model(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> None:
    """Back to the agent's own model."""
    await session.execute(
        delete(ThreadModelSetting).where(
            ThreadModelSetting.tenant_id == tenant_id,
            ThreadModelSetting.platform == platform,
            ThreadModelSetting.thread_id == thread_id,
        )
    )
    await session.flush()
