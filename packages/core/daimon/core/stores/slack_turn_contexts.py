"""Async store for slack_turn_contexts — leak-policy destination source.

The Slack adapter inserts a row before run_turn and deletes it in finally.
Readers pass a cutoff so rows from a crashed process age out fail-closed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import SlackTurnContext
from daimon.core.stores.domain import SlackTurnContextRow
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def create_slack_turn_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    channel_id: str,
    thread_ts: str,
    started_at: datetime,
    id: uuid.UUID | None = None,
) -> SlackTurnContextRow:
    orm = SlackTurnContext(
        id=id if id is not None else uuid.uuid4(),
        tenant_id=tenant_id,
        account_id=account_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        started_at=started_at,
    )
    session.add(orm)
    await session.flush()
    return SlackTurnContextRow.model_validate(orm)


async def delete_slack_turn_context(session: AsyncSession, *, id: uuid.UUID) -> int:
    """Delete one turn-context row. Idempotent; returns rowcount."""
    result = await session.execute(delete(SlackTurnContext).where(SlackTurnContext.id == id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def delete_turn_contexts_for_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Delete every slack_turn_contexts row for an account. Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.
    """
    result = await session.execute(
        delete(SlackTurnContext).where(
            SlackTurnContext.tenant_id == tenant_id,
            SlackTurnContext.account_id == account_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_turn_contexts_for_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Read-only count for the purge-cascade preview."""
    result = await session.execute(
        select(func.count())
        .select_from(SlackTurnContext)
        .where(
            SlackTurnContext.tenant_id == tenant_id,
            SlackTurnContext.account_id == account_id,
        )
    )
    return result.scalar_one()


async def get_slack_turn_channels(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    cutoff: datetime,
) -> frozenset[str]:
    """Distinct channels with a non-stale live turn for this account."""
    result = await session.execute(
        select(SlackTurnContext.channel_id)
        .where(
            SlackTurnContext.tenant_id == tenant_id,
            SlackTurnContext.account_id == account_id,
            SlackTurnContext.started_at >= cutoff,
        )
        .distinct()
    )
    return frozenset(str(channel_id) for channel_id in result.scalars().all())


async def get_slack_turn_destination(
    session: AsyncSession,
    *,
    id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    cutoff: datetime,
) -> str | None:
    """Resolve only the row named by a verified execution credential."""
    return (
        await session.execute(
            select(SlackTurnContext.channel_id).where(
                SlackTurnContext.id == id,
                SlackTurnContext.tenant_id == tenant_id,
                SlackTurnContext.account_id == account_id,
                SlackTurnContext.started_at >= cutoff,
            )
        )
    ).scalar_one_or_none()
