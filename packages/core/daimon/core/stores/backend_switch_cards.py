"""Provider switches waiting on a Confirm card that Daimon posted itself.

A turn on a provider with no confirmation surface (OpenAI, Gemini) can't show
the switch card the way a Claude turn does, so the MCP tool records the
switch here and posts the card; the platform's click handler claims the row
once, re-verifies everything and applies. A row is only ever a request: what
it names is checked again at the click.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from daimon.core._models import BackendSwitchCard
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

SwitchCardStatus = Literal["pending", "claimed", "applied", "refused", "cancelled"]


@dataclass(frozen=True)
class SwitchCardRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    channel_id: str
    thread_id: str
    setup_thread: bool
    requester_platform_user_id: str
    answering_agent_name: str | None
    reference: dict[str, Any]
    status: SwitchCardStatus
    outcome: str | None
    message_id: str | None
    expires_at: datetime


def _row(record: BackendSwitchCard) -> SwitchCardRow:
    return SwitchCardRow(
        id=record.id,
        tenant_id=record.tenant_id,
        account_id=record.account_id,
        platform=record.platform,
        channel_id=record.channel_id,
        thread_id=record.thread_id,
        setup_thread=record.setup_thread,
        requester_platform_user_id=record.requester_platform_user_id,
        answering_agent_name=record.answering_agent_name,
        reference=dict(record.reference),
        status=record.status,  # pyright: ignore[reportArgumentType]
        outcome=record.outcome,
        message_id=record.message_id,
        expires_at=record.expires_at,
    )


async def create_card(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    setup_thread: bool,
    requester_platform_user_id: str,
    answering_agent_name: str | None,
    reference: dict[str, Any],
    expires_at: datetime,
) -> SwitchCardRow:
    record = BackendSwitchCard(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        setup_thread=setup_thread,
        requester_platform_user_id=requester_platform_user_id,
        answering_agent_name=answering_agent_name,
        reference=reference,
        expires_at=expires_at,
        status="pending",
    )
    session.add(record)
    await session.flush()
    await session.refresh(record)
    return _row(record)


async def get_card(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID
) -> SwitchCardRow | None:
    record = await session.scalar(
        select(BackendSwitchCard).where(
            BackendSwitchCard.id == card_id, BackendSwitchCard.tenant_id == tenant_id
        )
    )
    return _row(record) if record is not None else None


async def mark_posted(session: AsyncSession, *, card_id: uuid.UUID, message_id: str) -> None:
    await session.execute(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == card_id)
        .values(message_id=message_id)
    )


async def claim(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID, now: datetime
) -> SwitchCardRow | None:
    """Take a pending, unexpired card for one click; None when another click or time won."""
    record = await session.scalar(
        update(BackendSwitchCard)
        .where(
            BackendSwitchCard.id == card_id,
            BackendSwitchCard.tenant_id == tenant_id,
            BackendSwitchCard.status == "pending",
            BackendSwitchCard.expires_at > now,
        )
        .values(status="claimed")
        .returning(BackendSwitchCard)
    )
    return _row(record) if record is not None else None


async def finish(
    session: AsyncSession,
    *,
    card_id: uuid.UUID,
    status: Literal["applied", "refused", "cancelled"],
    outcome: str,
) -> None:
    """Record how a card ended. Only a claimed card (or a pending one cancelled) ends."""
    allowed = ("claimed", "pending") if status == "cancelled" else ("claimed",)
    await session.execute(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == card_id, BackendSwitchCard.status.in_(allowed))
        .values(status=status, outcome=outcome)
    )
