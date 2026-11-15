"""Imported one-shot timers, held until an operator confirms them for an agent."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from daimon.core._models import DirectMessageConversation, HeldTimer
from daimon.core.continuity.timers import MAX_PENDING_TIMERS
from daimon.core.stores.direct_messages import DM_SCOPE_PREFIX
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import (
    count_pending_timer_rows,
    lock_timer_quota,
    record_continuation,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

if TYPE_CHECKING:
    from daimon.core.continuity.watch_imports import ImportedTimer
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: A timer closer than this to firing is dropped: nobody can act on it.
MIN_LEAD = timedelta(minutes=1)

Outcome = Literal["scheduled", "past", "capped", "no_dm"]


async def import_held_timer(
    session: AsyncSession, definition: ImportedTimer, *, tenant_id: uuid.UUID, now: datetime
) -> Literal["held", "existing", "past"]:
    """Hold one timer. A rerun adds nothing; a timer already due is recorded as settled."""
    principal = await get_or_create_platform_principal(
        session,
        tenant_id=tenant_id,
        platform="discord",
        external_id=definition.owner_discord_id,
    )
    past = definition.fire_at < now + MIN_LEAD
    result = await session.execute(
        insert(HeldTimer)
        .values(
            tenant_id=tenant_id,
            source_id=definition.source_id,
            account_id=principal.account_id,
            external_user_id=definition.owner_discord_id,
            platform="discord",
            channel_id=definition.channel_id,
            thread_id=definition.thread_id,
            place_type=definition.place_type,
            fire_at=definition.fire_at,
            note="" if past else definition.note,
            settled_at=now if past else None,
            outcome="past" if past else None,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "source_id"])
        .returning(HeldTimer.source_id)
    )
    if result.scalar_one_or_none() is None:
        return "existing"
    return "past" if past else "held"


async def settle_held_timers(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    agent_name: str,
    now: datetime,
) -> Counter[Outcome]:
    """Schedule each held timer for the agent, once. Each settles in its own transaction."""
    async with sessionmaker() as session:
        keys = list(
            await session.scalars(
                select(HeldTimer.source_id)
                .where(HeldTimer.tenant_id == tenant_id, HeldTimer.settled_at.is_(None))
                .order_by(HeldTimer.fire_at, HeldTimer.source_id)
            )
        )
    counts: Counter[Outcome] = Counter()
    for source_id in keys:
        async with sessionmaker.begin() as session:
            timer = await session.get(HeldTimer, (tenant_id, source_id), with_for_update=True)
            if timer is None or timer.settled_at is not None:
                continue
            outcome = await _settle_one(
                session, timer, agent_id=agent_id, agent_name=agent_name, now=now
            )
            timer.settled_at = now
            timer.outcome = outcome
            timer.note = ""
            counts[outcome] += 1
    return counts


async def _settle_one(
    session: AsyncSession, timer: HeldTimer, *, agent_id: str, agent_name: str, now: datetime
) -> Outcome:
    if timer.fire_at < now + MIN_LEAD:
        return "past"
    parent = timer.channel_id
    thread = timer.thread_id or timer.channel_id
    if timer.place_type == "direct-message":
        # Only the owner's existing private conversation here can receive it.
        scope = await session.scalar(
            select(DirectMessageConversation.scope_id)
            .where(
                DirectMessageConversation.tenant_id == timer.tenant_id,
                DirectMessageConversation.account_id == timer.account_id,
                DirectMessageConversation.scope_id.startswith(DM_SCOPE_PREFIX),
            )
            .order_by(DirectMessageConversation.scope_id)
            .limit(1)
        )
        if scope is None:
            return "no_dm"
        parent = thread = scope
    await lock_timer_quota(session, tenant_id=timer.tenant_id, account_id=timer.account_id)
    pending = await count_pending_timer_rows(
        session, tenant_id=timer.tenant_id, requester_account_id=timer.account_id
    )
    if pending >= MAX_PENDING_TIMERS:
        return "capped"
    await record_continuation(
        session,
        tenant_id=timer.tenant_id,
        platform="discord",
        parent_channel_id=parent,
        thread_id=thread,
        requester_account_id=timer.account_id,
        requester_external_user_id=timer.external_user_id,
        target_ma_agent_id=agent_id,
        target_name=agent_name,
        reason="timer",
        idempotency_key=uuid.uuid4(),
        requested_work=timer.note,
        available_at=timer.fire_at,
    )
    return "scheduled"


__all__ = ["MIN_LEAD", "import_held_timer", "settle_held_timers"]
