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
from pydantic import BaseModel
from sqlalchemy import ScalarSelect, delete, func, select, update
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
    client: bool,
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
        client=client,
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


async def waiting_for(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_platform_user_id: str,
    agent_name: str,
    now: datetime,
) -> bool:
    """Whether this requester already has an unanswered, unexpired card for this agent."""
    found = await session.scalar(
        select(BackendSwitchCard.id)
        .where(
            BackendSwitchCard.tenant_id == tenant_id,
            BackendSwitchCard.requester_platform_user_id == requester_platform_user_id,
            BackendSwitchCard.reference["agent"].astext == agent_name,
            BackendSwitchCard.status.in_(("pending", "claimed")),
            BackendSwitchCard.expires_at > now,
        )
        .limit(1)
    )
    return found is not None


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


async def record_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
    requester_platform_user_id: str,
    agent_name: str,
    stale_digest: str,
    fresh: BaseModel,
    expires_at: datetime,
    operation: str = "switch",
) -> None:
    """A switch card re-previewed when it was built (J5): the stale preview it was asked
    with, and the one it showed. Never pressable (no message) and never a waiting card
    (no top-level agent); the approved call spends it once (`consume_refresh`)."""
    # Every refresh that expired unspent goes first: nothing else sweeps them.
    await session.execute(
        delete(BackendSwitchCard).where(
            BackendSwitchCard.reference.has_key("refresh"),
            BackendSwitchCard.status.in_(("pending", "claimed")),
            BackendSwitchCard.expires_at <= func.now(),
        )
    )
    fresh_reference = fresh.model_dump(mode="json")
    record = BackendSwitchCard(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        requester_platform_user_id=requester_platform_user_id,
        reference={
            "refresh": {
                "operation": operation,
                "agent": agent_name,
                "of": stale_digest,
                "fresh": fresh_reference["preview_digest"],
                "revision": fresh_reference["expected_revision"],
            },
            "reference": fresh_reference,
        },
        expires_at=expires_at,
        status="pending",
    )
    session.add(record)
    await session.flush()


def _refresh_row(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str | None,
    stale_digest: str,
    fresh_digest: str,
    fresh_revision: int | str,
    status: str,
    now: datetime,
    operation: str,
) -> ScalarSelect[uuid.UUID]:
    """One live refresh row for this account, agent and stale -> fresh preview."""
    return (
        select(BackendSwitchCard.id)
        .where(
            BackendSwitchCard.tenant_id == tenant_id,
            BackendSwitchCard.account_id == account_id,
            BackendSwitchCard.reference.contains(
                {
                    "refresh": {
                        "operation": operation,
                        **({"agent": agent_name} if agent_name is not None else {}),
                        "of": stale_digest,
                        "fresh": fresh_digest,
                        "revision": fresh_revision,
                    }
                }
            ),
            BackendSwitchCard.status == status,
            BackendSwitchCard.expires_at > now,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )


async def approve_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    stale_digest: str,
    fresh_digest: str,
    fresh_revision: int | str,
    now: datetime,
    operation: str = "switch",
) -> bool:
    """The person pressed Confirm on a re-previewed card: its refresh may now be spent
    (pending -> claimed). A denied or expired card's refresh never is."""
    target = _refresh_row(
        tenant_id,
        account_id,
        agent_name,
        stale_digest,
        fresh_digest,
        fresh_revision,
        "pending",
        now,
        operation,
    )
    approved = await session.scalar(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == target, BackendSwitchCard.status == "pending")
        .values(status="claimed")
        .returning(BackendSwitchCard.id)
    )
    return approved is not None


async def cancel_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    stale_digest: str,
    fresh_digest: str,
    fresh_revision: int | str,
    now: datetime,
    operation: str = "switch",
) -> None:
    """A Confirm passed the click check, but its card was then already answered or
    expired: the refresh it approved is never spent (claimed -> cancelled)."""
    target = _refresh_row(
        tenant_id,
        account_id,
        None,
        stale_digest,
        fresh_digest,
        fresh_revision,
        "claimed",
        now,
        operation,
    )
    await session.execute(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == target, BackendSwitchCard.status == "claimed")
        .values(status="cancelled", outcome="Its card was already answered or expired.")
    )


async def consume_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    stale_digest: str,
    fresh_digest: str,
    fresh_revision: int | str,
    now: datetime,
    operation: str = "switch",
) -> bool:
    """Spend one approved card refresh: the approved call's stale preview, re-previewed to
    exactly the agent's current one. False when none matches (the agent changed again,
    or that card was denied or never confirmed)."""
    target = _refresh_row(
        tenant_id,
        account_id,
        agent_name,
        stale_digest,
        fresh_digest,
        fresh_revision,
        "claimed",
        now,
        operation,
    )
    spent = await session.scalar(
        update(BackendSwitchCard)
        .where(BackendSwitchCard.id == target, BackendSwitchCard.status == "claimed")
        .values(status="applied", outcome="Switched by a card re-previewed when it was built.")
        .returning(BackendSwitchCard.id)
    )
    return spent is not None
