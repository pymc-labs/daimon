"""Per-turn memory grants: read only by the provider-turn credential's jti."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import TurnMemoryGrant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class TurnMemoryGrantRow:
    jti: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID
    agent_name: str
    personal: bool
    read_only: bool
    is_dm: bool
    platform: str
    channel_id: str
    thread_id: str | None
    expires_at: datetime


async def put_turn_memory_grant(session: AsyncSession, grant: TurnMemoryGrantRow) -> None:
    """Record a turn's grant beside its freshly minted credential (same transaction)."""
    session.add(
        TurnMemoryGrant(
            jti=grant.jti,
            tenant_id=grant.tenant_id,
            agent_id=grant.agent_id,
            account_id=grant.account_id,
            agent_name=grant.agent_name,
            personal=grant.personal,
            read_only=grant.read_only,
            is_dm=grant.is_dm,
            platform=grant.platform,
            channel_id=grant.channel_id,
            thread_id=grant.thread_id,
            expires_at=grant.expires_at,
        )
    )
    await session.flush()


async def get_turn_memory_grant(
    session: AsyncSession, *, jti: uuid.UUID
) -> TurnMemoryGrantRow | None:
    row = await session.scalar(select(TurnMemoryGrant).where(TurnMemoryGrant.jti == jti))
    if row is None:
        return None
    return TurnMemoryGrantRow(
        jti=row.jti,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        account_id=row.account_id,
        agent_name=row.agent_name,
        personal=row.personal,
        read_only=row.read_only,
        is_dm=row.is_dm,
        platform=row.platform,
        channel_id=row.channel_id,
        thread_id=row.thread_id,
        expires_at=row.expires_at,
    )
