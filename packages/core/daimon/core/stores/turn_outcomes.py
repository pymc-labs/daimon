"""Content-free, idempotent turn outcomes. No prompt/answer/error messages accepted."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from daimon.core._models import TurnOutcome
from daimon.core.context_prompt import TurnContext

if TYPE_CHECKING:
    from daimon.core.turn.termination import TerminationReason
from pydantic import JsonValue
from sqlalchemy import ColumnElement, DateTime, and_, func, literal, not_, select
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
    eval_key: dict[str, JsonValue] | None = None
    is_dm: bool = False


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


# A turn counts as an answer the way `turn.user_failure` gates it: not a command-line run,
# and not a boundary's catch-all (reason unknown with no error class).
def _counted() -> ColumnElement[bool]:
    return and_(
        TurnOutcome.platform != "cli",
        not_(and_(TurnOutcome.reason == "unknown", TurnOutcome.error_class.is_(None))),
    )


def _failed() -> ColumnElement[bool]:
    from daimon.core.turn.termination import USER_VISIBLE_FAILURES

    return TurnOutcome.reason.in_(sorted(r.value for r in USER_VISIBLE_FAILURES))


@dataclass(frozen=True)
class AnswerStats:
    answers: int
    failed: int
    busy: int
    recovered: int
    p50_ms: float | None
    p95_ms: float | None
    cost_usd: Decimal
    costed: int
    """Completed answers the cost average is over: those with a known cost."""


async def answer_stats(
    session: AsyncSession, *, tenant_id: UUID, since: datetime, until: datetime
) -> AnswerStats:
    """Counted turns started in `[since, until)`: answers, failures, busy turn-aways,
    retries that recovered, time percentiles and cost of the completed ones."""
    completed = TurnOutcome.reason == "completed"
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(_failed()),
                func.count().filter(
                    TurnOutcome.reason.in_(("admission_concurrency_shed", "session_busy"))
                ),
                func.count().filter(TurnOutcome.recovered),
                func.percentile_cont(0.5).within_group(TurnOutcome.duration_ms).filter(completed),
                func.percentile_cont(0.95).within_group(TurnOutcome.duration_ms).filter(completed),
                func.coalesce(func.sum(TurnOutcome.cost_usd).filter(completed), 0),
                func.count().filter(and_(completed, TurnOutcome.cost_usd.is_not(None))),
            ).where(
                TurnOutcome.tenant_id == tenant_id,
                TurnOutcome.started_at >= since,
                TurnOutcome.started_at < until,
                _counted(),
            )
        )
    ).one()
    return AnswerStats(
        answers=int(row[0]),
        failed=int(row[1]),
        busy=int(row[2]),
        recovered=int(row[3]),
        p50_ms=None if row[4] is None else float(row[4]),
        p95_ms=None if row[5] is None else float(row[5]),
        cost_usd=Decimal(row[6]),
        costed=int(row[7]),
    )


async def answer_series(
    session: AsyncSession, *, tenant_id: UUID, now: datetime, span: timedelta, buckets: int
) -> list[tuple[int, int]]:
    """`(answers, failed)` per `span`, `buckets` of them oldest first, the last ending at `now`."""
    age = func.greatest(
        0,
        func.floor(
            func.extract("epoch", literal(now, DateTime(timezone=True)) - TurnOutcome.started_at)
            / span.total_seconds()
        ),
    )
    rows = await session.execute(
        select(age.label("age"), func.count(), func.count().filter(_failed()))
        .where(
            TurnOutcome.tenant_id == tenant_id,
            TurnOutcome.started_at >= now - span * buckets,
            _counted(),
        )
        .group_by(age)
    )
    out = [(0, 0)] * buckets
    for steps, answers, failed in rows:
        position = buckets - 1 - int(steps)
        if 0 <= position < buckets:
            out[position] = (int(answers), int(failed))
    return out
