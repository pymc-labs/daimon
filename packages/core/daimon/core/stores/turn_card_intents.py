"""Durable initial status-card intents for Discord and Slack turns."""

from __future__ import annotations

import uuid
from typing import cast

from daimon.core._models import TurnCardIntent
from daimon.core.stores.domain import TurnCardIntentRow
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


class TurnCardIntentConflictError(ValueError):
    """The same tenant-scoped turn token was reused with a different address."""


async def create_turn_card_intent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    turn_token: uuid.UUID,
    channel_id: str | None = None,
) -> TurnCardIntentRow:
    """Create or return the intent identified by this turn's stable token.

    The caller must commit this row before posting to the platform. `flush()`
    alone is not durable and cannot make the intent visible to a boot sweep.
    Reusing a token is idempotent when the platform address is unchanged.
    """
    statement = (
        insert(TurnCardIntent)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            turn_token=turn_token,
            channel_id=channel_id,
        )
        .on_conflict_do_nothing(
            index_elements=[TurnCardIntent.tenant_id, TurnCardIntent.turn_token]
        )
    )
    await session.execute(statement)
    orm = (
        await session.execute(
            select(TurnCardIntent).where(
                TurnCardIntent.tenant_id == tenant_id,
                TurnCardIntent.turn_token == turn_token,
            )
        )
    ).scalar_one()
    if (orm.platform, orm.thread_id, orm.channel_id) != (platform, thread_id, channel_id):
        raise TurnCardIntentConflictError("turn token was reused with a different platform address")
    return TurnCardIntentRow.model_validate(orm)


async def record_turn_card_message(
    session: AsyncSession,
    *,
    id: uuid.UUID,
    message_id: str,
) -> bool:
    """Record a successful platform post without overwriting another result.

    Returns True when this call records the ID or confirms the same ID was
    already recorded. A retired row or a different recorded ID returns False.
    """
    result = await session.execute(
        update(TurnCardIntent)
        .where(
            TurnCardIntent.id == id,
            TurnCardIntent.status == "prepared",
            TurnCardIntent.message_id.is_(None),
        )
        .values(status="posted", message_id=message_id, updated_at=func.now())
    )
    if cast(CursorResult[object], result).rowcount == 1:
        await session.flush()
        return True
    existing = (
        await session.execute(select(TurnCardIntent).where(TurnCardIntent.id == id))
    ).scalar_one_or_none()
    await session.flush()
    return (
        existing is not None and existing.status == "posted" and existing.message_id == message_id
    )


async def retire_turn_card_intent(
    session: AsyncSession,
    *,
    id: uuid.UUID,
    expected_message_id: str | None,
) -> bool:
    """Retire only the active intent whose stored message ID matches the read.

    `None` matches only a still-prepared intent with no recorded message ID.
    Choosing when that is safe belongs to the adapter recovery policy.
    """
    result = await session.execute(
        update(TurnCardIntent)
        .where(
            TurnCardIntent.id == id,
            TurnCardIntent.status.in_(("prepared", "posted")),
            TurnCardIntent.message_id.is_not_distinct_from(expected_message_id),
        )
        .values(status="retired", updated_at=func.now())
    )
    await session.flush()
    return cast(CursorResult[object], result).rowcount == 1


async def list_recoverable_turn_card_intents(
    session: AsyncSession,
    *,
    platform: str,
) -> list[TurnCardIntentRow]:
    """List every non-retired intent for one adapter's boot recovery.

    Prepared rows with a NULL message ID are included deliberately. They mark
    the post/response-persistence crash window, whose recovery behavior is an
    adapter decision. Rows are not claimed or retired by this read.
    """
    rows = (
        await session.execute(
            select(TurnCardIntent)
            .where(
                TurnCardIntent.platform == platform,
                TurnCardIntent.status.in_(("prepared", "posted")),
            )
            .order_by(TurnCardIntent.created_at, TurnCardIntent.id)
        )
    ).scalars()
    return [TurnCardIntentRow.model_validate(row) for row in rows]
