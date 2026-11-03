"""Per-turn usage events store. BILL-01.

Cost is computed at read time against MODEL_PRICING, and provider models against
the dated tariff they settled at — repricing is a query change, no backfill
needed. Writes are idempotent on (managed_session_id, event_id) so SSE replay is
a no-op.

Per `guideline:architecture` Error Propagation: this module does NOT
swallow exceptions — the cma predecessor's `try/except: log; return` pattern
is dropped.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import date, datetime, timedelta
from typing import Any, cast

from daimon.core._models import TurnOutcome, UsageEvent
from daimon.core.pricing import (
    LONG_CONTEXT_PROMPT_TOKENS,
    MODEL_PRICING,
    PROVIDER_PRICED_INPUT_LIMITS,
    LegacyUsage,
    UsageTokens,
    cost_of_bucket,
    provider_cost_of_bucket,
    provider_price_on,
    usage_tokens,
)
from daimon.core.stores.domain import UsageEventRow
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from sqlalchemy import ColumnElement, Date, Select, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    managed_session_id: str,
    model: str,
    model_usage: LegacyUsage | None = None,
    observation: UsageObservation | None = None,
    event_id: str,
    channel_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Insert one usage row idempotently on (managed_session_id, event_id).

    `agent_id` is the agent whose turn or tool call spent it; None when unknown."""
    if observation is not None:
        if model_usage is not None:
            raise ValueError("supply either an observation or legacy usage")
        model_usage = usage_tokens(observation)
    if model_usage is None:
        raise ValueError("usage rows require all four reported token stages")
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
            agent_id=agent_id,
        )
        .on_conflict_do_nothing(
            index_elements=["managed_session_id", "event_id"],
        )
    )
    await session.execute(stmt)
    await session.flush()


async def project_revision(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    observation: UsageObservation,
    initial: bool,
    channel_id: str | None,
    projected_tokens: UsageTokens | None = None,
    agent_id: str | None = None,
) -> None:
    """Project complete effective counts, preserving the original row identity.

    Partial measurements live only in the neutral store: legacy integer columns
    cannot represent unknown. An older pending row may still debit its delta,
    but never rewinds the latest projected counts.
    """
    if observation.model is None:
        raise ValueError("a billable usage observation requires a model")
    model = observation.model.id
    tokens = projected_tokens if projected_tokens is not None else usage_tokens(observation)
    where = (
        UsageEvent.managed_session_id == observation.session.id,
        UsageEvent.event_id == observation.id,
    )

    def check(existing: UsageEvent) -> None:
        if (
            existing.tenant_id != tenant_id
            or existing.platform_user_id != platform_user_id
            or existing.channel_id != channel_id
            or existing.model != model
        ):
            raise ScopeViolation(observation.id, "usage row belongs to another billing context")
        if (
            initial
            and (
                existing.observation_revision is None
                or existing.observation_revision <= observation.revision
            )
            and (
                observation.revision != 1
                or tokens is None
                or (
                    existing.input_tokens,
                    existing.output_tokens,
                    existing.cache_creation_input_tokens,
                    existing.cache_read_input_tokens,
                )
                != (
                    tokens.input_tokens,
                    tokens.output_tokens,
                    tokens.cache_creation_input_tokens,
                    tokens.cache_read_input_tokens,
                )
            )
        ):
            raise ValueError("historical usage must be adopted from its unchanged first revision")

    if tokens is None:
        existing = await session.scalar(select(UsageEvent).where(*where).with_for_update())
        if existing is not None:
            check(existing)
        return
    values = dict(
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_creation_input_tokens=tokens.cache_creation_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
        observation_revision=observation.revision,
    )
    inserted = await session.scalar(
        pg_insert(UsageEvent)
        .values(
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=observation.session.id,
            model=model,
            event_id=observation.id,
            channel_id=channel_id,
            agent_id=agent_id,
            **values,
        )
        .on_conflict_do_nothing(index_elements=["managed_session_id", "event_id"])
        .returning(UsageEvent.id)
    )
    if inserted is not None:
        return
    # Read after the insert has waited for any concurrent legacy writer. Lock
    # and validate before updating, including a historical baseline adoption.
    existing = await session.scalar(select(UsageEvent).where(*where).with_for_update())
    assert existing is not None
    check(existing)
    if (
        existing.observation_revision is None
        or existing.observation_revision < observation.revision
    ):
        await session.execute(update(UsageEvent).where(*where).values(**values))


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
    """Whether one row's prompt crossed its model's long-context threshold.

    A provider model's threshold is its tariff's priced input limit; every other
    model's is LONG_CONTEXT_PROMPT_TOKENS.
    """
    limits = {model: n for model, n in PROVIDER_PRICED_INPUT_LIMITS.items() if n is not None}
    threshold = case(limits, value=UsageEvent.model, else_=LONG_CONTEXT_PROMPT_TOKENS)
    return (
        UsageEvent.input_tokens
        + UsageEvent.cache_creation_input_tokens
        + UsageEvent.cache_read_input_tokens
        > threshold
    ).label("long_prompt")


def _tariff_day() -> ColumnElement[date | None]:
    """The UTC day a provider model's row is priced on; NULL for list-priced models.

    Provider tariffs are dated, so their rows are bucketed per day.
    """
    day = func.date(func.timezone("UTC", UsageEvent.occurred_at), type_=Date)
    return case(dict.fromkeys(PROVIDER_PRICED_INPUT_LIMITS, day), value=UsageEvent.model).label(
        "tariff_day"
    )


def _bucket_cost(model: str, day: date | None, usage: UsageTokens, *, long_prompt: bool) -> float:
    """One bucket's cost before markup, as the ledger debited it; 0 when unpriced."""
    if day is None:
        return cost_of_bucket(usage, MODEL_PRICING.get(model), long_prompt=long_prompt) or 0.0
    price = provider_price_on(model, day)
    cost = None if price is None else provider_cost_of_bucket(usage, price, long_prompt=long_prompt)
    return 0.0 if cost is None else float(cost)


async def _costs_by_keys(
    session: AsyncSession,
    keys: Sequence[ColumnElement[Any] | InstrumentedAttribute[Any]],
    *where: ColumnElement[bool],
) -> dict[tuple[Any, ...], float]:
    """Spend of the matching rows by the `keys` columns (one empty-tuple total without any).

    Priced per (model, long-prompt tier, tariff day) bucket: cost is linear in each
    token count within a bucket, so summing tokens in SQL and pricing each bucket once
    equals pricing every row, without loading a window's worth of rows on the per-turn
    cap gate. Buckets must not merge tiers: summing first would push short requests
    into the long tier.
    """
    long_prompt, day = _long_prompt(), _tariff_day()
    groups: list[ColumnElement[Any] | InstrumentedAttribute[Any]] = [
        UsageEvent.model,
        long_prompt,
        day,
        *keys,
    ]
    stmt = (
        select(
            *groups,
            func.sum(UsageEvent.input_tokens).label("in_tok"),
            func.sum(UsageEvent.output_tokens).label("out_tok"),
            func.sum(UsageEvent.cache_creation_input_tokens).label("cw_tok"),
            func.sum(UsageEvent.cache_read_input_tokens).label("cr_tok"),
        )
        .where(*where)
        .group_by(*groups)
    )
    costs: dict[tuple[Any, ...], float] = {}
    for row in (await session.execute(stmt)).all():
        usage = UsageTokens(
            input_tokens=int(row.in_tok or 0),
            output_tokens=int(row.out_tok or 0),
            cache_creation_input_tokens=int(row.cw_tok or 0),
            cache_read_input_tokens=int(row.cr_tok or 0),
        )
        name = tuple(row[3 : 3 + len(keys)])
        cost = _bucket_cost(row.model, row.tariff_day, usage, long_prompt=bool(row.long_prompt))
        costs[name] = costs.get(name, 0.0) + cost
    return costs


async def _costs_by(
    session: AsyncSession,
    key: InstrumentedAttribute[str | None] | None,
    *where: ColumnElement[bool],
) -> dict[str | None, float]:
    """Spend of the matching rows by `key` (all under None without one); see `_costs_by_keys`."""
    costs = await _costs_by_keys(session, [] if key is None else [key], *where)
    return {(name[0] if name else None): cost for name, cost in costs.items()}


async def _cost_since(session: AsyncSession, *where: ColumnElement[bool]) -> float:
    """Sum of every matching row's cost."""
    return sum((await _costs_by(session, None, *where)).values(), 0.0)


async def cost_for_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
    agent_id: str | None = None,
) -> float:
    """One person's spend since `since`; with `agent_id`, only on that agent."""
    agent_filter = [] if agent_id is None else [UsageEvent.agent_id == agent_id]
    return await _cost_since(
        session,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id == platform_user_id,
        UsageEvent.occurred_at >= since,
        *agent_filter,
    )


async def cost_for_users_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_ids: Collection[str],
    since: datetime,
    agent_id: str | None = None,
) -> float:
    """One person's spend since `since` across every id they spend under."""
    agent_filter = [] if agent_id is None else [UsageEvent.agent_id == agent_id]
    return await _cost_since(
        session,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id.in_(list(platform_user_ids)),
        UsageEvent.occurred_at >= since,
        *agent_filter,
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
    until: datetime | None = None,
) -> float:
    """Total tenant spend since `since` (before `until`, when given), including rows
    with no person recorded.

    Those rows were debited too (usage settled after its turn, calls made for no
    one), so the total matches the ledger and the agents add up to it; the
    per-person reads alone leave them out. Reprices in Python at read time.
    """
    return await _cost_since(session, UsageEvent.tenant_id == tenant_id, *_window(since, until))


async def turn_count_for_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> int:
    """Distinct managed_session count for the tenant since `since`.

    Counts rows with no person recorded, as `cost_for_tenant_since` does.
    """
    stmt = select(func.count(func.distinct(UsageEvent.managed_session_id))).where(
        UsageEvent.tenant_id == tenant_id, *_window(since, until)
    )
    return int((await session.execute(stmt)).scalar_one())


def _window(since: datetime, until: datetime | None) -> list[ColumnElement[bool]]:
    """`occurred_at` from `since`, up to but not including `until` when given."""
    window = [UsageEvent.occurred_at >= since]
    if until is not None:
        window.append(UsageEvent.occurred_at < until)
    return window


async def costs_by_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, float]:
    """Per-user spend for a tenant since `since` (before `until`, when given).

    EXCLUDES NULL platform_user_id.

    Pair with `turns_by_user_in_tenant_since` to assemble per-user (cost, turns)
    rows — turn counts cannot be derived from this aggregate without
    double-counting sessions that span multiple models.
    """
    costs = await _costs_by(
        session,
        UsageEvent.platform_user_id,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id.is_not(None),
        *_window(since, until),
    )
    return {user_id: cost for user_id, cost in costs.items() if user_id is not None}


async def turns_by_user_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, int]:
    """Per-user distinct-session count for a tenant since `since` (before `until`).

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
            *_window(since, until),
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


async def costs_by_agent_for_user_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, float]:
    """One person's spend since `since`, by agent (provider cost, before markup)."""
    return await _costs_by(
        session,
        UsageEvent.agent_id,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id == platform_user_id,
        *_window(since, until),
    )


async def costs_by_agent_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, float]:
    """A tenant's spend since `since`, by agent (provider cost, before markup).

    `None` keys spend no agent was recorded for. Counts rows with no person
    recorded, so the agents add up to `cost_for_tenant_since`.
    """
    return await _costs_by(
        session, UsageEvent.agent_id, UsageEvent.tenant_id == tenant_id, *_window(since, until)
    )


async def _turns_by_agent(
    session: AsyncSession, *where: ColumnElement[bool]
) -> dict[str | None, int]:
    """Distinct managed sessions per agent for the matching rows; None keys unattributed."""
    stmt = (
        select(
            UsageEvent.agent_id,
            func.count(func.distinct(UsageEvent.managed_session_id)).label("turns"),
        )
        .where(*where)
        .group_by(UsageEvent.agent_id)
    )
    return {row.agent_id: int(row.turns) for row in (await session.execute(stmt)).all()}


async def turns_by_agent_for_user_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, int]:
    """One person's turn count since `since`, by agent; pairs with the cost read."""
    return await _turns_by_agent(
        session,
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.platform_user_id == platform_user_id,
        *_window(since, until),
    )


async def turns_by_agent_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, int]:
    """A tenant's turn count since `since`, by agent, rows with no person included."""
    return await _turns_by_agent(session, UsageEvent.tenant_id == tenant_id, *_window(since, until))


async def costs_by_user_with_unrecorded_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, float]:
    """A tenant's spend by user since `since` (before `until`); `None` keys spend no
    person was recorded for, so the users add up to `cost_for_tenant_since`."""
    return await _costs_by(
        session,
        UsageEvent.platform_user_id,
        UsageEvent.tenant_id == tenant_id,
        *_window(since, until),
    )


async def turns_by_user_with_unrecorded_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[str | None, int]:
    """Distinct sessions per user for a tenant, `None` for spend with no person recorded."""
    stmt = (
        select(
            UsageEvent.platform_user_id,
            func.count(func.distinct(UsageEvent.managed_session_id)).label("turns"),
        )
        .where(UsageEvent.tenant_id == tenant_id, *_window(since, until))
        .group_by(UsageEvent.platform_user_id)
    )
    return {row.platform_user_id: int(row.turns) for row in (await session.execute(stmt)).all()}


#: How long before a window a turn may have started and still spend inside it.
_TURN_START_MARGIN = timedelta(days=1)


def _origin(tenant_id: uuid.UUID, since: datetime) -> ColumnElement[str]:
    """`routine` for rows spent in a session a routine run opened, else `chat`.

    A routine run opens its own session, so the turn outcome that names it says which
    rows are routine spend without a join on the usage refs.
    """
    routine_sessions = select(TurnOutcome.session_id).where(
        TurnOutcome.tenant_id == tenant_id,
        TurnOutcome.origin == "routine",
        TurnOutcome.session_id.is_not(None),
        TurnOutcome.started_at >= since - _TURN_START_MARGIN,
    )
    return case((UsageEvent.managed_session_id.in_(routine_sessions), "routine"), else_="chat")


async def costs_by_channel_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[tuple[str | None, str], float]:
    """A tenant's spend by `(channel_id, origin)`, rows with no channel under `None`.

    Counts every row, as `cost_for_tenant_since` does, so the channels add up to it.
    """
    costs = await _costs_by_keys(
        session,
        [UsageEvent.channel_id, _origin(tenant_id, since).label("origin")],
        UsageEvent.tenant_id == tenant_id,
        *_window(since, until),
    )
    return {(channel, origin): cost for (channel, origin), cost in costs.items()}


async def channel_costs_by_day_agent_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_ids: Sequence[str] = (),
    channel_prefix: str | None = None,
    since: datetime,
) -> dict[tuple[date, str | None, str | None], float]:
    """Spend on the channels (or ids under `channel_prefix`) by (UTC day, agent, person)."""
    places: list[ColumnElement[bool]] = []
    if channel_ids:
        places.append(UsageEvent.channel_id.in_(channel_ids))
    if channel_prefix is not None:
        places.append(UsageEvent.channel_id.startswith(channel_prefix, autoescape=True))
    if not places:
        return {}
    day = func.date(func.timezone("UTC", UsageEvent.occurred_at)).label("spend_day")
    costs = await _costs_by_keys(
        session,
        [day, UsageEvent.agent_id, UsageEvent.platform_user_id],
        UsageEvent.tenant_id == tenant_id,
        or_(*places),
        *_window(since, None),
    )
    return {(d, agent, user): cost for (d, agent, user), cost in costs.items()}


async def turns_by_channel_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[tuple[str | None, str], int]:
    """Distinct sessions per `(channel_id, origin)`; pairs with the cost read."""
    origin = _origin(tenant_id, since).label("origin")
    stmt = (
        select(
            UsageEvent.channel_id,
            origin,
            func.count(func.distinct(UsageEvent.managed_session_id)).label("turns"),
        )
        .where(UsageEvent.tenant_id == tenant_id, *_window(since, until))
        .group_by(UsageEvent.channel_id, origin)
    )
    return {
        (row.channel_id, row.origin): int(row.turns) for row in (await session.execute(stmt)).all()
    }


async def costs_by_day_in_tenant_since(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> dict[date, float]:
    """A tenant's spend per UTC day (provider cost, before markup); days without use are absent."""
    day = func.date(func.timezone("UTC", UsageEvent.occurred_at), type_=Date).label("day")
    costs = await _costs_by_keys(
        session, [day], UsageEvent.tenant_id == tenant_id, *_window(since, until)
    )
    return {key[0]: cost for key, cost in costs.items()}
