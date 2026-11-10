"""Gated tool calls waiting on the requester's Approve card (`daimon.core.host_calls`).

A provider-hosted turn can't show a confirmation card, so the MCP gate records the
call here and Daimon posts the card itself. Every step is one conditional UPDATE, so
a double press or two consumers move a row exactly once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from daimon.core._models import HostCallCard
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

HostCallStatus = Literal[
    "pending", "approved", "executing", "applied", "failed", "denied", "cancelled", "expired"
]

Finished = Literal["applied", "failed"]

# Open asks one origin may hold. Denied cards count too, so a model that keeps asking
# after a No can't flood the thread with cards.
MAX_OPEN_PER_ORIGIN = 5


@dataclass(frozen=True)
class HostCallCardRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    origin_id: uuid.UUID
    requester_account_id: uuid.UUID
    requester_platform_user_id: str
    platform: str
    channel_id: str
    thread_id: str
    executing_agent_id: uuid.UUID
    executing_ma_agent_id: str
    agent_name: str
    tool_name: str
    args_digest: str
    reference: dict[str, Any]
    status: HostCallStatus
    outcome: str | None
    message_id: str | None
    expires_at: datetime


def _row(record: HostCallCard) -> HostCallCardRow:
    return HostCallCardRow(
        id=record.id,
        tenant_id=record.tenant_id,
        origin_id=record.origin_id,
        requester_account_id=record.requester_account_id,
        requester_platform_user_id=record.requester_platform_user_id,
        platform=record.platform,
        channel_id=record.channel_id,
        thread_id=record.thread_id,
        executing_agent_id=record.executing_agent_id,
        executing_ma_agent_id=record.executing_ma_agent_id,
        agent_name=record.agent_name,
        tool_name=record.tool_name,
        args_digest=record.args_digest,
        reference=dict(record.reference),
        status=record.status,  # pyright: ignore[reportArgumentType]
        outcome=record.outcome,
        message_id=record.message_id,
        expires_at=record.expires_at,
    )


@dataclass(frozen=True)
class Created:
    row: HostCallCardRow
    # False when a retry of the same pending call found the existing card.
    fresh: bool


async def create_card(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    requester_platform_user_id: str,
    platform: str,
    channel_id: str,
    thread_id: str,
    executing_agent_id: uuid.UUID,
    executing_ma_agent_id: str,
    agent_name: str,
    tool_name: str,
    args_digest: str,
    reference: dict[str, Any],
    expires_at: datetime,
) -> Created | None:
    """Record one gated call; None when the origin already holds its cap of open asks."""
    existing = await _pending(session, origin_id, tool_name, args_digest)
    if existing is not None:
        return Created(existing, fresh=False)
    open_count = await session.scalar(
        select(func.count())
        .select_from(HostCallCard)
        .where(
            HostCallCard.origin_id == origin_id,
            HostCallCard.status.in_(("pending", "denied")),
        )
    )
    if (open_count or 0) >= MAX_OPEN_PER_ORIGIN:
        return None
    inserted = await session.scalar(
        insert(HostCallCard)
        .values(
            tenant_id=tenant_id,
            origin_id=origin_id,
            requester_account_id=requester_account_id,
            requester_platform_user_id=requester_platform_user_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            executing_agent_id=executing_agent_id,
            executing_ma_agent_id=executing_ma_agent_id,
            agent_name=agent_name,
            tool_name=tool_name,
            args_digest=args_digest,
            reference=reference,
            expires_at=expires_at,
            status="pending",
        )
        .on_conflict_do_nothing(
            index_elements=["origin_id", "tool_name", "args_digest"],
            # A literal: Postgres matches a partial index only on its exact predicate.
            index_where=text("status = 'pending'"),
        )
        .returning(HostCallCard)
    )
    if inserted is not None:
        return Created(_row(inserted), fresh=True)
    # A concurrent retry inserted the same call between the lookup and the insert.
    raced = await _pending(session, origin_id, tool_name, args_digest)
    return Created(raced, fresh=False) if raced is not None else None


async def _pending(
    session: AsyncSession, origin_id: uuid.UUID, tool_name: str, args_digest: str
) -> HostCallCardRow | None:
    record = await session.scalar(
        select(HostCallCard).where(
            HostCallCard.origin_id == origin_id,
            HostCallCard.tool_name == tool_name,
            HostCallCard.args_digest == args_digest,
            HostCallCard.status == "pending",
        )
    )
    return _row(record) if record is not None else None


async def get_card(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID
) -> HostCallCardRow | None:
    record = await session.scalar(
        select(HostCallCard).where(HostCallCard.id == card_id, HostCallCard.tenant_id == tenant_id)
    )
    return _row(record) if record is not None else None


async def mark_posted(session: AsyncSession, *, card_id: uuid.UUID, message_id: str) -> None:
    await session.execute(
        update(HostCallCard).where(HostCallCard.id == card_id).values(message_id=message_id)
    )


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    to: Literal["approved", "denied"],
    now: datetime,
    outcome: str | None = None,
) -> HostCallCardRow | None:
    """Answer a pending, unexpired card once; None when another press or time won."""
    record = await session.scalar(
        update(HostCallCard)
        .where(
            HostCallCard.id == card_id,
            HostCallCard.tenant_id == tenant_id,
            HostCallCard.status == "pending",
            HostCallCard.expires_at > now,
        )
        .values(status=to, outcome=outcome)
        .returning(HostCallCard)
    )
    return _row(record) if record is not None else None


async def cancel(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID, outcome: str
) -> bool:
    """Cancel a card nobody answered; False when a press claimed it first."""
    cancelled = await session.scalar(
        update(HostCallCard)
        .where(
            HostCallCard.id == card_id,
            HostCallCard.tenant_id == tenant_id,
            HostCallCard.status == "pending",
        )
        .values(status="cancelled", outcome=outcome)
        .returning(HostCallCard.id)
    )
    return cancelled is not None


async def list_approved(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    executing_agent_id: uuid.UUID,
) -> list[HostCallCardRow]:
    """Approved calls one wake turn runs, oldest first."""
    records = await session.scalars(
        select(HostCallCard)
        .where(
            HostCallCard.tenant_id == tenant_id,
            HostCallCard.platform == platform,
            HostCallCard.thread_id == thread_id,
            HostCallCard.executing_agent_id == executing_agent_id,
            HostCallCard.status == "approved",
        )
        .order_by(HostCallCard.created_at)
    )
    return [_row(r) for r in records]


async def consume(
    session: AsyncSession,
    *,
    card_id: uuid.UUID,
    tool_name: str,
    args_digest: str,
    executing_agent_id: uuid.UUID,
    now: datetime,
) -> HostCallCardRow | None:
    """Take an approved call to run it; None unless tool, args and agent all match."""
    record = await session.scalar(
        update(HostCallCard)
        .where(
            HostCallCard.id == card_id,
            HostCallCard.status == "approved",
            HostCallCard.tool_name == tool_name,
            HostCallCard.args_digest == args_digest,
            HostCallCard.executing_agent_id == executing_agent_id,
        )
        .values(status="executing", executing_at=now)
        .returning(HostCallCard)
    )
    return _row(record) if record is not None else None


async def finish(
    session: AsyncSession,
    *,
    card_id: uuid.UUID,
    status: Finished,
    outcome: str,
) -> None:
    await session.execute(
        update(HostCallCard)
        .where(HostCallCard.id == card_id, HostCallCard.status == "executing")
        .values(status=status, outcome=outcome)
    )


async def fail_approved(session: AsyncSession, *, card_id: uuid.UUID, outcome: str) -> bool:
    """An approved call the wake could not run at all (no credential, no server)."""
    failed = await session.scalar(
        update(HostCallCard)
        .where(HostCallCard.id == card_id, HostCallCard.status == "approved")
        .values(status="failed", outcome=outcome)
        .returning(HostCallCard.id)
    )
    return failed is not None


async def sweep(
    session: AsyncSession, *, now: datetime, executing_before: datetime, crashed_outcome: str
) -> int:
    """Expire unanswered cards and fail calls a crashed worker left executing.

    A stuck `executing` row may or may not have run, so it is failed, never retried.
    """
    expired = await session.execute(
        update(HostCallCard)
        .where(HostCallCard.status == "pending", HostCallCard.expires_at <= now)
        .values(status="expired")
        .returning(HostCallCard.id)
    )
    stuck = await session.execute(
        update(HostCallCard)
        .where(HostCallCard.status == "executing", HostCallCard.executing_at <= executing_before)
        .values(status="failed", outcome=crashed_outcome)
        .returning(HostCallCard.id)
    )
    return len(expired.all()) + len(stuck.all())
