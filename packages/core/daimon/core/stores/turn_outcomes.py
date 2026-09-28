"""Content-free, idempotent turn outcomes. No prompt/answer/error messages accepted."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from daimon.core._models import TurnOutcome
from daimon.core.context_prompt import TurnContext

if TYPE_CHECKING:
    from daimon.core.turn.termination import TerminationReason
from sqlalchemy import select
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
    return [OutcomeRecord(**dict(row)) for row in rows]
