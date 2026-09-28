"""One-shot timers: the first source on the wake queue.

"Remind me in two hours", "check back tomorrow at nine": the agent leaves
itself a note, and at the given instant a turn runs in the same thread with
that note as its message. A timer is nothing but a wake
(`daimon.core.continuity.wakes`) with `reason='timer'` and
`available_at = fire_at`, so it inherits the queue's guarantees — it survives
restarts, runs at most once, goes through the same admission as a mention —
and resumes the thread's bound session, so the conversation's context carries
over. Creation counts and inserts under a per-person advisory lock, so the
pending cap holds under concurrent calls. Cancelling is `cancel_wake`: a
status flip, so a cancelled timer can never be claimed.

Recurring asks are routines, not timers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from daimon.core.continuity.wakes import cancel_wake
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import (
    count_pending_timer_rows,
    get_continuation,
    list_pending_timer_rows,
    lock_timer_quota,
    record_continuation,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "MAX_PENDING_TIMERS",
    "MAX_TIMER_NOTE",
    "TIMER_MAX_DELAY",
    "TIMER_MIN_DELAY",
    "TimerError",
    "cancel_timer",
    "list_timers",
    "parse_fire_at",
    "schedule_timer",
]

#: Nearer than this is not a timer, it is the current turn.
TIMER_MIN_DELAY: Final[timedelta] = timedelta(minutes=1)

#: Further out than this, the conversation it resumes is unlikely to matter.
TIMER_MAX_DELAY: Final[timedelta] = timedelta(days=90)

#: The note is the whole brief the future turn gets besides the thread.
MAX_TIMER_NOTE: Final[int] = 2000

#: Pending timers one person may hold in one workspace. Stops a looping agent
#: from filling the queue; nobody needs more reminders than this at once.
MAX_PENDING_TIMERS: Final[int] = 25


class TimerError(DaimonError):
    """A timer request that cannot be scheduled; the message is model-facing."""


def parse_fire_at(value: str, *, now: datetime) -> datetime:
    """An ISO 8601 instant with an explicit UTC offset, inside the allowed window.

    A naive time is refused rather than guessed: "9am" means a different
    instant for every person, and the offset is what the agent must settle.
    """
    try:
        fire_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TimerError(
            f"fire_at {value!r} is not an ISO 8601 time; use e.g. 2026-09-29T09:00:00+02:00."
        ) from exc
    if fire_at.tzinfo is None or fire_at.utcoffset() is None:
        raise TimerError(
            "fire_at needs a UTC offset (e.g. +00:00); work out the person's timezone first."
        )
    fire_at = fire_at.astimezone(UTC)
    if fire_at < now + TIMER_MIN_DELAY:
        raise TimerError("fire_at must be at least one minute from now.")
    if fire_at > now + TIMER_MAX_DELAY:
        raise TimerError("fire_at must be within 90 days.")
    return fire_at


async def schedule_timer(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack"],
    parent_channel_id: str,
    thread_id: str,
    requester_account_id: uuid.UUID,
    requester_external_user_id: str,
    target_ma_agent_id: str,
    target_name: str,
    note: str,
    fire_at: datetime,
) -> uuid.UUID:
    """Queue a timer; its id (the wake's idempotency key) is what cancels it."""
    note = note.strip()
    if not note:
        raise TimerError("note is empty; say what to do when the timer fires.")
    if len(note) > MAX_TIMER_NOTE:
        raise TimerError(f"note is longer than {MAX_TIMER_NOTE} characters; shorten it.")
    timer_id = uuid.uuid4()
    # Count and insert under one per-person lock, so two concurrent creators
    # cannot both see room for the last timer.
    async with sessionmaker.begin() as session:
        await lock_timer_quota(session, tenant_id=tenant_id, account_id=requester_account_id)
        pending = await count_pending_timer_rows(
            session, tenant_id=tenant_id, requester_account_id=requester_account_id
        )
        if pending >= MAX_PENDING_TIMERS:
            raise TimerError(
                f"This person already has {MAX_PENDING_TIMERS} pending timers; cancel one first."
            )
        await record_continuation(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            requester_account_id=requester_account_id,
            requester_external_user_id=requester_external_user_id,
            target_ma_agent_id=target_ma_agent_id,
            target_name=target_name,
            reason="timer",
            idempotency_key=timer_id,
            requested_work=note,
            available_at=fire_at,
        )
    return timer_id


async def list_timers(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> list[TaskContinuationRow]:
    """This person's timers that have not fired or been cancelled, soonest first."""
    async with sessionmaker() as session:
        return await list_pending_timer_rows(
            session, tenant_id=tenant_id, requester_account_id=account_id
        )


async def cancel_timer(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    timer_id: uuid.UUID,
    account_id: uuid.UUID,
    is_admin: bool,
) -> bool:
    """Cancel a pending timer this caller set (or any, for an admin).

    False when there is no such pending timer for the caller — unknown,
    another person's, already fired, or already cancelled look the same, so a
    probe learns nothing about other people's timers.
    """
    async with sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=timer_id)
    if row is None or row.tenant_id != tenant_id or row.reason != "timer":
        return False
    if not is_admin and row.requester_account_id != account_id:
        return False
    return await cancel_wake(sessionmaker, tenant_id=tenant_id, idempotency_key=timer_id)
