"""Notices waiting for the next turn in a thread (`daimon.core.turn.agent_notices`)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core._models import AgentNotice
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_agent_notices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    texts: Sequence[str],
) -> None:
    """Queue notices for the thread's next turn; a text already waiting is kept once."""
    if not texts:
        return
    rows = [
        {"tenant_id": tenant_id, "platform": platform, "thread_id": thread_id, "text": text}
        for text in dict.fromkeys(texts)
    ]
    await session.execute(insert(AgentNotice).values(rows).on_conflict_do_nothing())


async def take_agent_notices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    since: datetime,
) -> list[str]:
    """Delete the thread's notices and return those from `since` on, oldest first."""
    taken = await session.execute(
        delete(AgentNotice)
        .where(
            AgentNotice.tenant_id == tenant_id,
            AgentNotice.platform == platform,
            AgentNotice.thread_id == thread_id,
        )
        .returning(AgentNotice.text, AgentNotice.created_at)
    )
    fresh = sorted((row.created_at, row.text) for row in taken if row.created_at >= since)
    return [text for _, text in fresh]
