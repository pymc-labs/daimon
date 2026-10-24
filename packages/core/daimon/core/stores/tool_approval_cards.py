"""Approve cards Daimon posts itself for a gated call on an OpenAI/Gemini turn.

The card approves one exact call (tool name plus its arguments' digest) for one
requester in one thread; the agent's identical retry consumes the approval once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import ToolApprovalCard
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ToolApprovalRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    channel_id: str
    thread_id: str
    requester_platform_user_id: str
    tool_name: str
    args_digest: str
    status: str
    message_id: str | None
    expires_at: datetime
    approved_until: datetime | None


def _row(record: ToolApprovalCard) -> ToolApprovalRow:
    return ToolApprovalRow(
        id=record.id,
        tenant_id=record.tenant_id,
        account_id=record.account_id,
        platform=record.platform,
        channel_id=record.channel_id,
        thread_id=record.thread_id,
        requester_platform_user_id=record.requester_platform_user_id,
        tool_name=record.tool_name,
        args_digest=record.args_digest,
        status=record.status,
        message_id=record.message_id,
        expires_at=record.expires_at,
        approved_until=record.approved_until,
    )


async def create_card(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    requester_platform_user_id: str,
    tool_name: str,
    args_digest: str,
    expires_at: datetime,
) -> ToolApprovalRow:
    record = ToolApprovalCard(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        requester_platform_user_id=requester_platform_user_id,
        tool_name=tool_name,
        args_digest=args_digest,
        expires_at=expires_at,
        status="pending",
    )
    session.add(record)
    await session.flush()
    await session.refresh(record)
    return _row(record)


async def get_card(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID
) -> ToolApprovalRow | None:
    record = await session.scalar(
        select(ToolApprovalCard).where(
            ToolApprovalCard.id == card_id, ToolApprovalCard.tenant_id == tenant_id
        )
    )
    return _row(record) if record is not None else None


async def pending_for(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    tool_name: str,
    args_digest: str,
    now: datetime,
) -> bool:
    """Whether an unanswered, unexpired card already asks about this exact call."""
    found = await session.scalar(
        select(ToolApprovalCard.id)
        .where(
            ToolApprovalCard.tenant_id == tenant_id,
            ToolApprovalCard.account_id == account_id,
            ToolApprovalCard.tool_name == tool_name,
            ToolApprovalCard.args_digest == args_digest,
            ToolApprovalCard.status == "pending",
            ToolApprovalCard.expires_at > now,
        )
        .limit(1)
    )
    return found is not None


async def mark_posted(session: AsyncSession, *, card_id: uuid.UUID, message_id: str) -> None:
    await session.execute(
        update(ToolApprovalCard).where(ToolApprovalCard.id == card_id).values(message_id=message_id)
    )


async def abandon(session: AsyncSession, *, card_id: uuid.UUID) -> None:
    """A card whose post failed: never pressable, never waiting."""
    await session.execute(
        update(ToolApprovalCard)
        .where(ToolApprovalCard.id == card_id, ToolApprovalCard.status == "pending")
        .values(status="refused")
    )


async def approve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    now: datetime,
    until: datetime,
) -> bool:
    """Approve a pending, unexpired card once; False when time or another press won."""
    approved = await session.scalar(
        update(ToolApprovalCard)
        .where(
            ToolApprovalCard.id == card_id,
            ToolApprovalCard.tenant_id == tenant_id,
            ToolApprovalCard.status == "pending",
            ToolApprovalCard.expires_at > now,
        )
        .values(status="approved", approved_until=until)
        .returning(ToolApprovalCard.id)
    )
    return approved is not None


async def cancel(session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID) -> bool:
    cancelled = await session.scalar(
        update(ToolApprovalCard)
        .where(
            ToolApprovalCard.id == card_id,
            ToolApprovalCard.tenant_id == tenant_id,
            ToolApprovalCard.status == "pending",
        )
        .values(status="cancelled")
        .returning(ToolApprovalCard.id)
    )
    return cancelled is not None


async def consume(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    thread_id: str,
    tool_name: str,
    args_digest: str,
    now: datetime,
) -> bool:
    """Spend one live approval of this exact call by this requester in this thread."""
    target = (
        select(ToolApprovalCard.id)
        .where(
            ToolApprovalCard.tenant_id == tenant_id,
            ToolApprovalCard.account_id == account_id,
            ToolApprovalCard.thread_id == thread_id,
            ToolApprovalCard.tool_name == tool_name,
            ToolApprovalCard.args_digest == args_digest,
            ToolApprovalCard.status == "approved",
            ToolApprovalCard.approved_until > now,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    spent = await session.scalar(
        update(ToolApprovalCard)
        .where(ToolApprovalCard.id == target, ToolApprovalCard.status == "approved")
        .values(status="consumed")
        .returning(ToolApprovalCard.id)
    )
    return spent is not None
