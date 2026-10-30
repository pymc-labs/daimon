"""Notices waiting for an agent's next turn in a thread (`daimon.core.turn.agent_notices`)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core._models import AgentNotice
from sqlalchemy import delete, func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_agent_notices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    agent_id: str,
    texts: Sequence[str],
) -> None:
    """Queue notices for the agent's next turn here; a text already waiting is kept once, as new."""
    if not texts:
        return
    rows = [
        {
            "tenant_id": tenant_id,
            "platform": platform,
            "thread_id": thread_id,
            "agent_id": agent_id,
            "text": text,
        }
        for text in dict.fromkeys(texts)
    ]
    await session.execute(
        insert(AgentNotice)
        .values(rows)
        .on_conflict_do_update(
            constraint="uq_agent_notices_thread_agent_text", set_={"created_at": func.now()}
        )
    )


async def take_agent_notices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    agent_id: str,
    since: datetime,
) -> list[str]:
    """Take the agent's notices here from `since` on, oldest first; drop older ones."""
    taken = await session.execute(
        delete(AgentNotice)
        .where(
            AgentNotice.tenant_id == tenant_id,
            AgentNotice.platform == platform,
            AgentNotice.thread_id == thread_id,
            or_(AgentNotice.agent_id == agent_id, AgentNotice.created_at < since),
        )
        .returning(AgentNotice.text, AgentNotice.agent_id, AgentNotice.created_at)
    )
    fresh = sorted(
        (row.created_at, row.text)
        for row in taken
        if row.agent_id == agent_id and row.created_at >= since
    )
    return [text for _, text in fresh]
