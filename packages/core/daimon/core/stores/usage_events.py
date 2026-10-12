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
from daimon.core.pricing import (
    LONG_CONTEXT_PROMPT_TOKENS,
    MODEL_PRICING,
    cost_of_bucket,
)
from daimon.core.stores.domain import UsageEventRow
from sqlalchemy import ColumnElement, Select, delete, func, select
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
    channel_id: str | None = None,
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
            channel_id=channel_id,
        )
        .on_conflict_do_nothing(
            index_elements=["managed_session_id", "event_id"],
        )
    )
    await session.execute(stmt)
    await session.flush()


async def list_event_ids_for_session(
    session: AsyncSession, *, managed_session_id: str
) -> frozenset[str]:
    """Event ids already recorded for one managed session (unique-key index scan)."""
    result = await session.execute(
        select(UsageEvent.event_id).where(UsageEvent.managed_session_id == managed_session_id)
    )
    return frozenset(result.scalars().all())


async def recorded_among(
    session: AsyncSession, *, managed_session_id: str, event_ids: Sequence[str]
) -> frozenset[str]:
    """Which of `event_ids` already have a usage row for the session."""
    if not event_ids:
        return frozenset()
    result = await session.execute(
        select(UsageEvent.event_id).where(
            UsageEvent.managed_session_id == managed_session_id,
            UsageEvent.event_id.in_(event_ids),
        )
    )
    return frozenset(result.scalars().all())


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


async def list_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None = None,
    since: datetime | None = None,
) -> Sequence[UsageEventRow]:
    """Public row-list read (e.g. test assertions on individual event fields)."""
    return await _select_rows(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id, since=since
    )


def _long_prompt() -> ColumnElement[bool]:
    """Whether one row's request crossed the long-context threshold."""
    return (
        UsageEvent.input_tokens
        + UsageEvent.cache_creation_input_tokens
        + UsageEvent.cache_read_input_tokens
        > LONG_CONTEXT_PROMPT_TOKENS
    ).label("long_prompt")


async def _cost_since(session: AsyncSession, *where: ColumnElement[bool]) -> float:
    """Sum of every matching row's cost, priced per (model, long-prompt tier) bucket.

    Cost is linear in each token count within a bucket, so summing tokens in
    SQL and pricing each bucket once equals pricing every row; it spares the
    per-turn cap gate from loading a window's worth of rows. Buckets must not
    merge tiers: summing first would push short requests into the long tier.
    """
    long_prompt = _long_prompt()
    stmt = (
        select(
            UsageEvent.model,
            long_prompt,
            func.sum(UsageEvent.input_tokens).label("in_tok"),
            func.sum(UsageEvent.output_tokens).label("out_tok"),
            func.sum(UsageEvent.cache_creation_input_tokens).label("cw_tok"),
            func.sum(UsageEvent.cache_read_input_tokens).label("cr_tok"),
        )
        .where(*where)
        .group_by(UsageEvent.model, long_prompt)
    )
    total = 0.0
    for row in (await session.execute(stmt)).all():
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=int(row.in_tok),
            output_tokens=int(row.out_tok),
            cache_creation_input_tokens=int(row.cw_tok),
            cache_read_input_tokens=int(row.cr_tok),
        )
        cost = cost_of_bucket(
            usage, MODEL_PRICING.get(row.model), long_prompt=bool(row.long_prompt)
        )
        if cost is not None:
            total += cost
    return total


async def cost_for_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
) -> float:
    return await _cost_since(
        session,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id == platform_user_id,
        UsageEvent.occurred_at >= since,
    )


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
    return await _cost_since(
        session,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id.is_not(None),
        UsageEvent.occurred_at >= since,
    )


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

    GROUP BY (platform_user_id, model, long-prompt tier) so each bucket is
    repriced in Python at its own rates: summing first and pricing the total
    would push short Haiku 5.5 requests into its long-context tier. Results
    are folded back to a per-user total. Pair with
    `turns_by_user_in_tenant_since` to assemble per-user (cost, turns) rows —
    turn counts cannot be derived from this aggregate without double-counting
    sessions that span multiple models.
    """
    long_prompt = _long_prompt()
    stmt = (
        select(
            UsageEvent.platform_user_id,
            UsageEvent.model,
            long_prompt,
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
        .group_by(UsageEvent.platform_user_id, UsageEvent.model, long_prompt)
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
        c = cost_of_bucket(usage, MODEL_PRICING.get(row.model), long_prompt=bool(row.long_prompt))
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


async def count_all(session: AsyncSession) -> int:
    """Return the total number of usage_events rows, unscoped by tenant.

    Read-only. Intended for regression tests asserting "no usage_events row
    was written anywhere" (e.g. a CLI path that must never bill) — production
    code should prefer the tenant-scoped helpers above.
    """
    return int((await session.execute(select(func.count()).select_from(UsageEvent))).scalar_one())
