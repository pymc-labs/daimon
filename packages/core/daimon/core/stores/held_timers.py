"""Imported one-shot timers, held until an operator confirms them for an agent."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple

from daimon.core._models import HeldTimer
from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX, DM_SCOPE_PREFIX
from daimon.core.continuity.timers import MAX_PENDING_TIMERS
from daimon.core.continuity.watch_imports import NOTE_LIMIT, clip
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import (
    count_pending_timer_rows,
    lock_timer_quota,
    record_continuation,
)
from sqlalchemy import (
    Boolean,
    ColumnElement,
    and_,
    delete,
    func,
    literal_column,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert

if TYPE_CHECKING:
    from daimon.core.continuity.watch_imports import ImportedTimer
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: A timer closer than this to firing is dropped: nobody can act on it.
MIN_LEAD = timedelta(minutes=1)

Outcome = Literal["scheduled", "past", "capped", "cancelled"]
ImportOutcome = Literal["held", "updated", "existing", "past", "unsupported"]

#: Where an imported timer can wake: an owner's private conversation, a thread or a channel.
SUPPORTED_PLACES = frozenset({"direct-message", "thread", "channel"})
#: What a timer with no note asks of the agent.
EMPTY_NOTE_PROMPT = "A reminder you set has come due."


class TimerImport(NamedTuple):
    outcome: ImportOutcome
    #: The note was longer than a stored one may be and was cut.
    truncated: bool = False


def supported(definition: ImportedTimer) -> bool:
    """A DM, or a thread or channel named by a numeric id."""
    if definition.place_type not in SUPPORTED_PLACES:
        return False
    if definition.place_type == "channel":
        return definition.channel_id.isdigit()
    return definition.place_type == "direct-message" or (
        (definition.thread_id or definition.channel_id).isdigit()
    )


async def import_held_timer(
    session: AsyncSession, definition: ImportedTimer, *, tenant_id: uuid.UUID, now: datetime
) -> TimerImport:
    """Hold one timer. A rerun updates one nobody has settled yet and adds nothing else.

    A timer already due is recorded as settled. A timer for a place that cannot wake is
    refused: it is counted, never stored. A rerun never changes `paused`:
    what the file says counts only the first time, so an operator's unpause stays.
    """
    if not supported(definition):
        return TimerImport("unsupported")
    note, truncated = clip(definition.note, NOTE_LIMIT)
    principal = await get_or_create_platform_principal(
        session,
        tenant_id=tenant_id,
        platform="discord",
        external_id=definition.owner_discord_id,
    )
    past = definition.fire_at < now + MIN_LEAD
    values = {
        "fire_at": definition.fire_at,
        "note": "" if past else note,
        "settled_at": now if past else None,
        "outcome": "past" if past else None,
    }
    statement = insert(HeldTimer).values(
        tenant_id=tenant_id,
        source_id=definition.source_id,
        account_id=principal.account_id,
        external_user_id=definition.owner_discord_id,
        platform="discord",
        channel_id=definition.channel_id,
        thread_id=definition.thread_id,
        place_type=definition.place_type,
        paused=definition.paused,
        **values,
    )
    result = (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "source_id"],
                set_=values,
                # Settled ones are final; an identical one is not a change.
                where=and_(
                    HeldTimer.settled_at.is_(None),
                    or_(
                        HeldTimer.fire_at != definition.fire_at,
                        HeldTimer.note != values["note"],
                    ),
                ),
            )
            # xmax is zero only on a row this statement inserted.
            .returning(literal_column("(xmax = 0)", Boolean))
        )
    ).first()
    if result is None:
        return TimerImport("existing", truncated)
    if past:
        return TimerImport("past", truncated)
    return TimerImport("held" if result[0] else "updated", truncated)


async def cancel_missing_timers(
    session: AsyncSession, *, tenant_id: uuid.UUID, keep: Collection[str], now: datetime
) -> int:
    """Cancel every unsettled timer whose source id is not in `keep`; how many were."""
    cancelled = (
        await session.execute(
            update(HeldTimer)
            .where(
                HeldTimer.tenant_id == tenant_id,
                HeldTimer.settled_at.is_(None),
                HeldTimer.source_id.not_in(list(keep)),
            )
            .values(settled_at=now, outcome="cancelled", note="")
            .returning(HeldTimer.source_id)
        )
    ).all()
    return len(cancelled)


async def unpause_timers(
    session: AsyncSession, *, tenant_id: uuid.UUID, source_id: str | None = None
) -> int:
    """Let held timers be scheduled by the next confirm: one by source id, or all."""
    condition = [
        HeldTimer.tenant_id == tenant_id,
        HeldTimer.settled_at.is_(None),
        HeldTimer.paused.is_(True),
    ]
    if source_id is not None:
        condition.append(HeldTimer.source_id == source_id)
    changed = (
        await session.execute(
            update(HeldTimer).where(*condition).values(paused=False).returning(HeldTimer.source_id)
        )
    ).all()
    return len(changed)


def _keys(users: Sequence[tuple[uuid.UUID, str, str]]) -> ColumnElement[bool]:
    return tuple_(HeldTimer.tenant_id, HeldTimer.platform, HeldTimer.external_user_id).in_(
        list(users)
    )


async def count_for_users(
    session: AsyncSession, *, users: Sequence[tuple[uuid.UUID, str, str]]
) -> int:
    """Rows held for these (tenant, platform, user) keys, settled ones included."""
    if not users:
        return 0
    return (
        await session.execute(select(func.count()).select_from(HeldTimer).where(_keys(users)))
    ).scalar_one()


async def delete_for_users(
    session: AsyncSession, *, users: Sequence[tuple[uuid.UUID, str, str]]
) -> int:
    """Delete rows held for these (tenant, platform, user) keys."""
    if not users:
        return 0
    removed = (
        await session.execute(delete(HeldTimer).where(_keys(users)).returning(HeldTimer.source_id))
    ).all()
    return len(removed)


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
                .where(
                    HeldTimer.tenant_id == tenant_id,
                    HeldTimer.settled_at.is_(None),
                    HeldTimer.paused.is_(False),
                )
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
        # The bot opens the owner's private conversation when it fires; this scope only
        # keeps each timer apart in the queue.
        parent = thread = f"{DM_SCOPE_PREFIX}{uuid.uuid4()}"
    elif timer.place_type == "channel":
        # The channel stays the parent; the scope keeps each timer apart in the queue.
        thread = f"{CHANNEL_SCOPE_PREFIX}{uuid.uuid4()}"
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
        requested_work=timer.note or EMPTY_NOTE_PROMPT,
        available_at=timer.fire_at,
    )
    return "scheduled"


__all__ = [
    "EMPTY_NOTE_PROMPT",
    "MIN_LEAD",
    "TimerImport",
    "cancel_missing_timers",
    "count_for_users",
    "delete_for_users",
    "import_held_timer",
    "settle_held_timers",
    "unpause_timers",
]
