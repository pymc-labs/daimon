"""Content-free, idempotent turn outcomes. No prompt/answer/error messages accepted."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from daimon.core._models import TurnOutcome
from daimon.core.context_prompt import TurnContext

if TYPE_CHECKING:
    from daimon.core.turn.termination import TerminationReason
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class OutcomeRecord:
    id: UUID
    tenant_id: UUID | None
    account_id: UUID | None
    platform: str
    channel_id: str | None
    thread_id: str | None
    agent_id: str | None
    session_id: str | None
    origin: TurnContext
    reason: TerminationReason
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    recovered: bool
    error_class: str | None
    release: str
    usage_refs: list[dict[str, str]]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    model_calls: int | None = None
    model_ids: list[str] | None = None
    cost_usd: Decimal | None = None
    unpriced_calls: int | None = None
    billing_posture: str | None = None


async def record(session: AsyncSession, outcome: OutcomeRecord) -> None:
    await session.execute(
        insert(TurnOutcome)
        .values(**asdict(outcome))
        .on_conflict_do_nothing(index_elements=[TurnOutcome.id])
    )


async def list_for_tenant(
    session: AsyncSession, tenant_id: UUID, *, limit: int = 100
) -> list[OutcomeRecord]:
    rows = (
        await session.execute(
            select(TurnOutcome.__table__)
            .where(TurnOutcome.tenant_id == tenant_id)
            .order_by(TurnOutcome.started_at.desc())
            .limit(limit)
        )
    ).mappings()
    from daimon.core.turn.termination import TerminationReason

    result: list[OutcomeRecord] = []
    for row in rows:
        values = dict(row)
        values["reason"] = TerminationReason(row["reason"])
        result.append(OutcomeRecord(**values))
    return result


async def session_ids_for_platform(session: AsyncSession, platform: str) -> list[str]:
    """Every MA session id an outcome on `platform` names, in id order."""
    rows = await session.execute(
        select(TurnOutcome.session_id)
        .where(TurnOutcome.platform == platform, TurnOutcome.session_id.is_not(None))
        .distinct()
        .order_by(TurnOutcome.session_id)
    )
    return [sid for sid in rows.scalars() if sid is not None]


async def last_started_at_many(
    session: AsyncSession, tenant_id: UUID, agent_ids: Collection[str]
) -> dict[str, datetime]:
    """`last_started_at` for many agents in one read; an agent that never started is absent."""
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(TurnOutcome.agent_id, func.max(TurnOutcome.started_at))
        .where(TurnOutcome.tenant_id == tenant_id, TurnOutcome.agent_id.in_(list(agent_ids)))
        .group_by(TurnOutcome.agent_id)
    )
    return {agent_id: at for agent_id, at in rows if at is not None}


async def last_started_at(session: AsyncSession, tenant_id: UUID, agent_id: str) -> datetime | None:
    """When ``agent_id`` last started a turn, or None when it never has."""
    return (
        await session.execute(
            select(func.max(TurnOutcome.started_at)).where(
                TurnOutcome.tenant_id == tenant_id, TurnOutcome.agent_id == agent_id
            )
        )
    ).scalar_one()
