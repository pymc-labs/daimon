"""Read-only per-turn telemetry queries. Billing remains authoritative elsewhere."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from daimon.core._models import TurnOutcome
from daimon.core.context_prompt import TurnContext
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


class TurnUsageRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    tenant_id: UUID | None
    account_id: UUID | None
    platform: str
    channel_id: str | None
    thread_id: str | None
    origin: TurnContext
    agent_id: str | None
    session_id: str | None
    reason: str
    started_at: datetime
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    model_calls: int | None
    model_ids: list[str] | None
    cost_usd: Decimal | None
    unpriced_calls: int | None
    billing_posture: str | None


class ChannelUsageRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    platform: str
    channel_id: str | None
    origin: TurnContext
    turns: int
    measured_turns: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    model_calls: int
    cost_usd: Decimal | None
    known_cost_usd: Decimal
    unpriced_calls: int


def _filters(
    tenant_id: UUID, since: datetime, channel_id: str | None, origin: TurnContext | None
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [
        TurnOutcome.tenant_id == tenant_id,
        TurnOutcome.started_at >= since,
    ]
    if channel_id is not None:
        filters.append(TurnOutcome.channel_id == channel_id)
    if origin is not None:
        filters.append(TurnOutcome.origin == origin)
    return filters


async def list_turn_usage(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    channel_id: str | None = None,
    origin: TurnContext | None = None,
    limit: int = 100,
) -> list[TurnUsageRow]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    stmt = (
        select(TurnOutcome.__table__)
        .where(*_filters(tenant_id, since, channel_id, origin))
        .order_by(TurnOutcome.started_at.desc(), TurnOutcome.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).mappings()
    return [TurnUsageRow.model_validate(dict(row)) for row in rows]


async def usage_by_channel(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    channel_id: str | None = None,
    origin: TurnContext | None = None,
) -> list[ChannelUsageRow]:
    known_cost = func.coalesce(func.sum(TurnOutcome.cost_usd), 0)
    unknown_cost = func.count().filter(TurnOutcome.cost_usd.is_(None))
    stmt = (
        select(
            TurnOutcome.platform,
            TurnOutcome.channel_id,
            TurnOutcome.origin,
            func.count().label("turns"),
            func.count(TurnOutcome.model_calls).label("measured_turns"),
            func.coalesce(func.sum(TurnOutcome.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TurnOutcome.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(TurnOutcome.cache_read_input_tokens), 0).label(
                "cache_read_input_tokens"
            ),
            func.coalesce(func.sum(TurnOutcome.cache_creation_input_tokens), 0).label(
                "cache_creation_input_tokens"
            ),
            func.coalesce(func.sum(TurnOutcome.model_calls), 0).label("model_calls"),
            case((unknown_cost > 0, None), else_=known_cost).label("cost_usd"),
            known_cost.label("known_cost_usd"),
            func.coalesce(func.sum(TurnOutcome.unpriced_calls), 0).label("unpriced_calls"),
        )
        .where(*_filters(tenant_id, since, channel_id, origin))
        .group_by(TurnOutcome.platform, TurnOutcome.channel_id, TurnOutcome.origin)
        .order_by(TurnOutcome.platform, TurnOutcome.channel_id, TurnOutcome.origin)
    )
    rows = (await session.execute(stmt)).mappings()
    return [ChannelUsageRow.model_validate(dict(row)) for row in rows]
