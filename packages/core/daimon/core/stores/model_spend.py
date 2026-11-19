"""Read observed daily model spend for ordinary turn admission."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.core._models import UsageObservation
from daimon.core.pricing import (
    ProviderPrice,
    minimum_cost_of,
    provider_cost_of,
    provider_price_on,
)
from mux.state.usage_ledger import AppliedUsage
from pydantic import ValidationError
from sqlalchemy import ColumnElement, DateTime, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


class SpendUnpriced(Exception):
    """Today's spend on this model can't be priced (no current tariff): fail closed."""


def utc_midnight(now: datetime) -> datetime:
    """The start of `now`'s UTC day, when every daily model cap resets."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def model_spend_today(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    model: str,
    now: datetime,
    rates: ProviderPrice | None,
) -> Decimal:
    """Observed usage today on `model` at list price.

    Each observation is priced by `provider_cost_of` and falls back to its token floor
    while it is still partial. Without a tariff current today the spend can't be known:
    `SpendUnpriced`, never $0. A malformed usage row is skipped and logged; it never
    stops a turn from starting.
    """
    if rates is None or not rates.current_on(now.date()):
        raise SpendUnpriced(model)
    observation = UsageObservation.applied["observation"]
    rows = await _latest_today(
        session,
        now,
        provider,
        UsageObservation.tenant_id == tenant_id,
        observation["model"]["id"].astext == model,
    )
    return sum((_cost(usage, rates) for usage in rows), Decimal(0))


async def provider_spend_today(session: AsyncSession, *, provider: str, now: datetime) -> Decimal:
    """Every tenant's observed usage today on `provider`'s models, at list price.

    Priced as `model_spend_today` prices one model. A model with no tariff current
    today is skipped and logged: no such model is run (`CappedFlashAccounting`, the
    OpenAI daily cap), so only usage from before its tariff lapsed is left out.
    """
    rows = await _latest_today(session, now, provider)
    spent = Decimal(0)
    for usage in rows:
        model = usage.observation.model.id if usage.observation.model is not None else None
        rates = provider_price_on(model, now.date()) if model is not None else None
        if rates is None:
            log.warning("model_spend.unpriced_usage_skipped", provider=provider, model=model)
            continue
        spent += _cost(usage, rates)
    return spent


async def _latest_today(
    session: AsyncSession, now: datetime, provider: str, *where: ColumnElement[bool]
) -> list[AppliedUsage]:
    """The newest revision of each `provider` observation observed since `now`'s UTC
    midnight, by the indexed columns; a row older code wrote without them, by its JSON."""
    observation = UsageObservation.applied["observation"]
    midnight = utc_midnight(now)
    today = or_(
        and_(UsageObservation.provider == provider, UsageObservation.observed_at >= midnight),
        and_(
            UsageObservation.provider.is_(None),
            observation["session"]["provider"].astext == provider,
            cast(observation["observed_at"].astext, DateTime(timezone=True)) >= midnight,
        ),
    )
    latest = (
        select(UsageObservation.applied)
        .where(today, *where)
        .distinct(UsageObservation.binding_id, UsageObservation.observation_id)
        .order_by(
            UsageObservation.binding_id,
            UsageObservation.observation_id,
            UsageObservation.revision.desc(),
        )
    )
    rows: list[AppliedUsage] = []
    for applied in (await session.execute(latest)).scalars():
        try:
            rows.append(AppliedUsage.model_validate(applied))
        except ValidationError:
            continue
    return rows


def _cost(usage: AppliedUsage, rates: ProviderPrice) -> Decimal:
    """Upper bound of one observation, else its token floor while partial; 0 if malformed."""
    effective = usage.observation.model_copy(update=dict(usage.tokens))
    try:
        cost = provider_cost_of(effective, rates, infrastructure_usd=Decimal(0))
        if cost is None:
            cost = minimum_cost_of(effective, rates)
    except ValueError:
        log.warning("model_spend.usage_row_skipped", model=rates.model)
        return Decimal(0)
    return cost or Decimal(0)
