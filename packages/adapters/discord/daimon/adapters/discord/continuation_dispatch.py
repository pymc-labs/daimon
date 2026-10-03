"""Discord I/O ports for the shared continuation and wake dispatcher."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from anthropic import AsyncAnthropic
from daimon.core.continuity.continuation import ContinuationDecision
from daimon.core.continuity.dispatch import dispatch_pending_continuations as dispatch_core
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)
__all__ = ["RunFollowUp", "dispatch_pending_continuations"]
RunFollowUp = Callable[[TaskContinuationRow, ContinuationDecision], Awaitable[None]]
MayPost = Callable[[], Awaitable[bool]]


async def _latest_human_message_at(thread: discord.Thread, *, after: datetime) -> datetime | None:
    """Newest human message timestamp in `thread` strictly after `after`, or None."""
    latest: datetime | None = None
    async for message in thread.history(limit=5):
        if message.author.bot:
            continue
        if message.created_at <= after:
            continue
        if latest is None or message.created_at > latest:
            latest = message.created_at
    return latest


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    thread: discord.Thread,
    run_follow_up: RunFollowUp,
    may_post: MayPost,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Dispatch under the caller's existing thread guard."""

    async def context(row: TaskContinuationRow) -> tuple[datetime | None, bool]:
        latest = await _latest_human_message_at(thread, after=row.created_at)
        async with sessionmaker() as session:
            live = await get_live_thread_session(
                session,
                tenant_id=row.tenant_id,
                platform="discord",
                thread_id=row.thread_id,
                account_id=row.requester_account_id,
            )
        return latest, live is not None and live.active_turn_message_id is not None

    async def post(row: TaskContinuationRow, text: str, reason: str) -> None:
        if await may_post():
            await thread.send(text)
        else:
            log.info("continuation.notice_withheld", thread_id=row.thread_id, reason=reason)

    await dispatch_core(
        sessionmaker,
        anthropic,
        tenant_id=tenant_id,
        platform="discord",
        thread_id=str(thread.id),
        run_decision=run_follow_up,
        decision_context=context,
        notice=post,
        settle_before_notice=False,
        dispatch_errors=(discord.HTTPException,),
        now=now,
    )
