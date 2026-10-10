"""Discord addressed-message dedupe and bounded startup history selection."""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from daimon.core._models import DiscordMessageAdmission, ThreadSession, TurnCardIntent
from daimon.core.stores.worker_ownership import owner_is_alive
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def claim_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    channel_id: str,
    message_id: str,
    owner_key: int,
    resume_owned: bool = False,
) -> bool:
    """Claim once, or reclaim an unhandled receipt whose process has exited.

    A process holds a session advisory lock for its entire lifetime. PostgreSQL
    releases it even on SIGKILL; no clock lease can mistake a slow turn for a
    dead process. The row lock serializes competing startup workers.
    """
    # Discord assigns the opening message ID to the thread it starts.
    # This also dedupes opening turns admitted before this ledger existed.
    if (
        await session.scalar(
            select(ThreadSession.id)
            .where(
                ThreadSession.tenant_id == tenant_id,
                ThreadSession.platform == "discord",
                ThreadSession.thread_id == message_id,
            )
            .limit(1)
        )
        is not None
    ):
        return False
    inserted = await session.scalar(
        insert(DiscordMessageAdmission)
        .values(
            tenant_id=tenant_id, channel_id=channel_id, message_id=message_id, owner_key=owner_key
        )
        .on_conflict_do_nothing()
        .returning(DiscordMessageAdmission.message_id)
    )
    if inserted is not None:
        return True
    row = await session.scalar(
        select(DiscordMessageAdmission)
        .where(
            DiscordMessageAdmission.tenant_id == tenant_id,
            DiscordMessageAdmission.message_id == message_id,
        )
        .with_for_update()
    )
    if row is None or row.handled:
        return False
    if row.owner_key == owner_key:
        # Only the local queue handoff uses this; gateway/replay duplicates
        # never resume a pending input that is already owned here.
        return resume_owned
    if await owner_is_alive(session, row.owner_key):
        return False
    row.owner_key = owner_key
    await session.flush()
    return True


async def finish_messages(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    message_ids: Iterable[str],
    intent_id: UUID | None = None,
) -> None:
    """Record delivery or a committed card intent before external side effects."""
    values: dict[str, object] = {"handled": True}
    if intent_id is not None:
        values["turn_card_intent_id"] = intent_id
    await session.execute(
        update(DiscordMessageAdmission)
        .where(
            DiscordMessageAdmission.tenant_id == tenant_id,
            DiscordMessageAdmission.message_id.in_(tuple(message_ids)),
        )
        .values(**values)
    )


async def replay_channels(
    session: AsyncSession, *, tenant_id: UUID, cutoff: datetime, limit: int
) -> list[tuple[str, str | None]]:
    """Recent channels plus active threads, newest activity first and bounded.

    Keep the oldest candidate watermark across callers. The replay also scans
    recent history for per-message receipts rather than assuming completion
    order equals message order (queued mentions can straddle an answer).
    """
    activity = select(
        DiscordMessageAdmission.channel_id.label("channel_id"),
        DiscordMessageAdmission.created_at.label("at"),
    ).where(
        DiscordMessageAdmission.tenant_id == tenant_id, DiscordMessageAdmission.created_at >= cutoff
    )
    threads = select(
        ThreadSession.thread_id.label("channel_id"), ThreadSession.updated_at.label("at")
    ).where(
        ThreadSession.tenant_id == tenant_id,
        ThreadSession.platform == "discord",
        ThreadSession.updated_at >= cutoff,
    )
    parents = select(
        ThreadSession.channel_id.label("channel_id"), ThreadSession.updated_at.label("at")
    ).where(
        ThreadSession.tenant_id == tenant_id,
        ThreadSession.platform == "discord",
        ThreadSession.updated_at >= cutoff,
        ThreadSession.channel_id.is_not(None),
    )
    recent = activity.union_all(threads, parents).subquery()
    channels = await session.scalars(
        select(recent.c.channel_id)
        .group_by(recent.c.channel_id)
        .order_by(func.max(recent.c.at).desc(), recent.c.channel_id)
        .limit(limit)
    )
    result: list[tuple[str, str | None]] = []
    for channel_id in channels:
        # Legacy sessions predate receipts. Their answer watermark is the safe
        # lower bound for first deployment of replay; unreceipted old mentions
        # before it must not be answered again.
        watermarks = await session.scalars(
            select(ThreadSession.watermark_message_id).where(
                ThreadSession.tenant_id == tenant_id,
                ThreadSession.platform == "discord",
                ThreadSession.thread_id == channel_id,
                ThreadSession.watermark_message_id.is_not(None),
            )
        )
        values = [value for value in watermarks if value is not None and value.isdecimal()]
        oldest_receipt = await session.scalar(
            select(DiscordMessageAdmission.message_id)
            .where(
                DiscordMessageAdmission.tenant_id == tenant_id,
                DiscordMessageAdmission.channel_id == channel_id,
                DiscordMessageAdmission.created_at >= cutoff,
            )
            .order_by(DiscordMessageAdmission.created_at)
            .limit(1)
        )
        if oldest_receipt is not None and oldest_receipt.isdecimal():
            values.append(str(int(oldest_receipt) - 1))
        result.append((str(channel_id), min(values, key=int) if values else None))
    return result


async def release_unposted_messages(session: AsyncSession, *, intent_id: UUID) -> None:
    """Requeue only a retired intent proven to have posted no card or MA turn."""
    empty_intent = select(TurnCardIntent.id).where(
        TurnCardIntent.id == intent_id,
        TurnCardIntent.status == "retired",
        TurnCardIntent.message_id.is_(None),
    )
    await session.execute(
        update(DiscordMessageAdmission)
        .where(
            DiscordMessageAdmission.turn_card_intent_id.in_(empty_intent),
        )
        .values(handled=False)
    )


async def has_released_messages(session: AsyncSession, *, intent_ids: Iterable[UUID]) -> bool:
    """Whether boot card reconciliation returned an input to the replay queue."""
    return (
        await session.scalar(
            select(DiscordMessageAdmission.message_id)
            .where(
                DiscordMessageAdmission.turn_card_intent_id.in_(tuple(intent_ids)),
                DiscordMessageAdmission.handled.is_(False),
            )
            .limit(1)
        )
        is not None
    )


async def unposted_thread_for_message(
    session: AsyncSession, *, tenant_id: UUID, message_id: str
) -> str | None:
    """Reuse the thread opened before a crash that never posted its first card."""
    return await session.scalar(
        select(TurnCardIntent.thread_id)
        .join(
            DiscordMessageAdmission,
            DiscordMessageAdmission.turn_card_intent_id == TurnCardIntent.id,
        )
        .where(
            DiscordMessageAdmission.tenant_id == tenant_id,
            DiscordMessageAdmission.message_id == message_id,
            TurnCardIntent.status == "retired",
            TurnCardIntent.message_id.is_(None),
        )
    )
