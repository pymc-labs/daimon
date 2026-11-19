"""Provider switch cards Daimon posted before switches applied at once.

No new card is made. A press on one still posted claims its row once and
applies the switch it names (`daimon.core.switch_cards`).
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
    client: bool
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
        client=record.client,
        answering_agent_name=record.answering_agent_name,
        reference=dict(record.reference),
        status=record.status,  # pyright: ignore[reportArgumentType]
        outcome=record.outcome,
        message_id=record.message_id,
        expires_at=record.expires_at,
    )


async def get_card(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID
) -> SwitchCardRow | None:
    record = await session.scalar(
        select(BackendSwitchCard).where(
            BackendSwitchCard.id == card_id, BackendSwitchCard.tenant_id == tenant_id
        )
    )
    return _row(record) if record is not None else None


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


async def cancel(
    session: AsyncSession, *, tenant_id: uuid.UUID, card_id: uuid.UUID, outcome: str
) -> bool:
    """Cancel a card nobody has pressed Confirm on; False when a Confirm claimed it first."""
    cancelled = await session.scalar(
        update(BackendSwitchCard)
        .where(
            BackendSwitchCard.id == card_id,
            BackendSwitchCard.tenant_id == tenant_id,
            BackendSwitchCard.status == "pending",
        )
        .values(status="cancelled", outcome=outcome)
        .returning(BackendSwitchCard.id)
    )
    return cancelled is not None


async def finish(
    session: AsyncSession,
    *,
    card_id: uuid.UUID,
    status: Literal["applied", "refused"],
    outcome: str,
) -> None:
    """Record how a claimed card ended; a card no Confirm claimed is never finished."""
    await session.execute(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == card_id, BackendSwitchCard.status == "claimed")
        .values(status=status, outcome=outcome)
    )
