"""Per-turn usage events store. BILL-01.

Cost is computed at read time against MODEL_PRICING — repricing is a query
change, no backfill needed. Writes are idempotent on
(managed_session_id, event_id) so SSE replay is a no-op.

Per `guideline:architecture` Error Propagation: this module does NOT
swallow exceptions — the cma predecessor's `try/except: log; return` pattern
is dropped.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core._models import UsageEvent
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores.domain import UsageEventRow
from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    managed_session_id: str,
    model: str,
    model_usage: BetaManagedAgentsSpanModelUsage,
    event_id: str,
) -> None:
    """Insert one usage row idempotently on (managed_session_id, event_id)."""
    stmt = (
        pg_insert(UsageEvent)
        .values(
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model,
            input_tokens=model_usage.input_tokens,
            output_tokens=model_usage.output_tokens,
            cache_creation_input_tokens=model_usage.cache_creation_input_tokens,
            cache_read_input_tokens=model_usage.cache_read_input_tokens,
            event_id=event_id,
        )
        .on_conflict_do_nothing(
            index_elements=["managed_session_id", "event_id"],
        )
    )
    await session.execute(stmt)
    await session.flush()


async def _select_rows(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None = None,
    since: datetime | None = None,
) -> Sequence[UsageEventRow]:
    stmt: Select[tuple[UsageEvent]] = select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)
    if platform_user_id is not None:
        stmt = stmt.where(UsageEvent.platform_user_id == platform_user_id)
    if since is not None:
        stmt = stmt.where(UsageEvent.occurred_at >= since)
    result = await session.execute(stmt)
    return [UsageEventRow.model_validate(r, from_attributes=True) for r in result.scalars().all()]


def _sum_cost(rows: Sequence[UsageEventRow]) -> float:
    """Reprice each row at query time against current MODEL_PRICING."""
    total = 0.0
    for row in rows:
        rates = MODEL_PRICING.get(row.model)
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_creation_input_tokens=row.cache_creation_input_tokens,
            cache_read_input_tokens=row.cache_read_input_tokens,
        )
        c = cost_of(usage, rates)
        if c is not None:
            total += c
    return total


async def cost_for_user_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> float:
    rows = await _select_rows(
        session,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
    )
    return _sum_cost(rows)


async def cost_for_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
) -> float:
    rows = await _select_rows(
        session,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        since=since,
    )
    return _sum_cost(rows)


async def turn_count_for_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
) -> int:
    """Distinct managed_session count for (tenant, user) since `since`.

    A "turn" is one managed_session. A single session may produce multiple
    usage_events rows (model-call fan-out); they collapse to one turn.
    """
    stmt = select(func.count(func.distinct(UsageEvent.managed_session_id))).where(
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id == platform_user_id,
        UsageEvent.occurred_at >= since,
    )
    return int((await session.execute(stmt)).scalar_one())


async def cost_for_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
) -> float:
    """Total tenant spend since `since`, EXCLUDING rows with NULL platform_user_id.

    Excludes NULL-attributed rows so the tenant total equals the sum of
    per-member rows. Reprices in Python at read time.
    """
    stmt = select(UsageEvent).where(
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id.is_not(None),
        UsageEvent.occurred_at >= since,
    )
    result = await session.execute(stmt)
    rows = [UsageEventRow.model_validate(r, from_attributes=True) for r in result.scalars().all()]
    return _sum_cost(rows)


async def turn_count_for_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
) -> int:
    """Distinct managed_session count for the tenant since `since`.

    EXCLUDES rows with NULL platform_user_id so the count matches the sum
    of per-member turn counts.
    """
    stmt = select(func.count(func.distinct(UsageEvent.managed_session_id))).where(
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id.is_not(None),
        UsageEvent.occurred_at >= since,
    )
    return int((await session.execute(stmt)).scalar_one())


async def costs_by_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
) -> dict[str, float]:
    """Per-user spend for a tenant since `since`. EXCLUDES NULL platform_user_id.

    GROUP BY (platform_user_id, model) so cost_of can reprice each model bucket
    in Python; results are folded back to a per-user total. Pair with
    `turns_by_user_in_tenant_since` to assemble per-user (cost, turns) rows —
    turn counts cannot be derived from this aggregate without double-counting
    sessions that span multiple models.
    """
    stmt = (
        select(
            UsageEvent.platform_user_id,
            UsageEvent.model,
            func.sum(UsageEvent.input_tokens).label("in_tok"),
            func.sum(UsageEvent.output_tokens).label("out_tok"),
            func.sum(UsageEvent.cache_creation_input_tokens).label("cw_tok"),
            func.sum(UsageEvent.cache_read_input_tokens).label("cr_tok"),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.platform_user_id.is_not(None),
            UsageEvent.occurred_at >= since,
        )
        .group_by(UsageEvent.platform_user_id, UsageEvent.model)
    )
    result = await session.execute(stmt)
    per_user: dict[str, float] = {}
    for row in result.all():
        user_id = row.platform_user_id
        if user_id is None:
            continue
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=int(row.in_tok or 0),
            output_tokens=int(row.out_tok or 0),
            cache_creation_input_tokens=int(row.cw_tok or 0),
            cache_read_input_tokens=int(row.cr_tok or 0),
        )
        c = cost_of(usage, MODEL_PRICING.get(row.model))
        per_user[user_id] = per_user.get(user_id, 0.0) + (c if c is not None else 0.0)
    return per_user


async def turns_by_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
) -> dict[str, int]:
    """Per-user distinct-session count for a tenant since `since`.

    EXCLUDES NULL platform_user_id. GROUP BY platform_user_id ONLY (not by
    model) so sessions spanning multiple models stay collapsed to one turn.
    """
    stmt = (
        select(
            UsageEvent.platform_user_id,
            func.count(func.distinct(UsageEvent.managed_session_id)).label("turns"),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.platform_user_id.is_not(None),
            UsageEvent.occurred_at >= since,
        )
        .group_by(UsageEvent.platform_user_id)
    )
    result = await session.execute(stmt)
    out: dict[str, int] = {}
    for row in result.all():
        if row.platform_user_id is None:
            continue
        out[row.platform_user_id] = int(row.turns)
    return out


async def cost_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> float:
    rows = await _select_rows(session, tenant_id=tenant_id)
    return _sum_cost(rows)


async def delete_all_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """GDPR purge. Returns rowcount; never raises on 0.

    `tenant_id` is required: `platform_user_id` is NOT globally unique — Slack
    user ids are workspace-scoped, so `U123` in two workspaces are two different
    humans. Deleting without a tenant filter would erase another tenant's rows.
    """
    result = await session.execute(
        delete(UsageEvent).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.platform_user_id == platform_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
