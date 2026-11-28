"""Store for the threads a workflow's posts opened, found again by their exact title."""

from __future__ import annotations

import uuid

from daimon.core._models import WorkflowThread
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def find_thread(
    session: AsyncSession, *, workflow_id: uuid.UUID, platform: str, channel_id: str, title: str
) -> str | None:
    return await session.scalar(
        select(WorkflowThread.thread_id).where(
            WorkflowThread.workflow_id == workflow_id,
            WorkflowThread.platform == platform,
            WorkflowThread.channel_id == channel_id,
            WorkflowThread.title == title,
        )
    )


async def record_thread(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    platform: str,
    channel_id: str,
    title: str,
    thread_id: str,
) -> str:
    """Keep the thread a post opened; when another post won the race, its thread stays."""
    await session.execute(
        insert(WorkflowThread)
        .values(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            platform=platform,
            channel_id=channel_id,
            title=title,
            thread_id=thread_id,
        )
        .on_conflict_do_nothing(constraint="uq_workflow_threads_title")
    )
    found = await find_thread(
        session, workflow_id=workflow_id, platform=platform, channel_id=channel_id, title=title
    )
    assert found is not None
    return found
