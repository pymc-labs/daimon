"""Read observed daily model spend for ordinary turn admission."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.core._models import UsageObservation
from daimon.core.pricing import ProviderPrice, minimum_cost_of, provider_cost_of
from mux.state.usage_ledger import AppliedUsage
from pydantic import ValidationError
from sqlalchemy import DateTime, cast, select
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

    Each observation is priced at its upper bound (`provider_cost_of`, so long-context
    rates count) and falls back to its token floor while it is still partial. Without
    a tariff current today the spend can't be known: `SpendUnpriced`, never $0. A
    malformed usage row is skipped and logged; it never stops a turn from starting.
    """
    if rates is None or not rates.current_on(now.date()):
        raise SpendUnpriced(model)
    observation = UsageObservation.applied["observation"]
    latest = (
        select(UsageObservation.applied)
        .where(
            UsageObservation.tenant_id == tenant_id,
            observation["session"]["provider"].astext == provider,
            observation["model"]["id"].astext == model,
            cast(observation["observed_at"].astext, DateTime(timezone=True)) >= utc_midnight(now),
        )
        .distinct(UsageObservation.binding_id, UsageObservation.observation_id)
        .order_by(
            UsageObservation.binding_id,
            UsageObservation.observation_id,
            UsageObservation.revision.desc(),
        )
    )
    spent = Decimal(0)
    for applied in (await session.execute(latest)).scalars():
        try:
            usage = AppliedUsage.model_validate(applied)
        except ValidationError:
            continue
        effective = usage.observation.model_copy(update=dict(usage.tokens))
        try:
            cost = provider_cost_of(effective, rates, infrastructure_usd=Decimal(0))
            if cost is None:
                cost = minimum_cost_of(effective, rates)
        except ValueError:
            log.warning("model_spend.usage_row_skipped", tenant_id=str(tenant_id), model=model)
            continue
        spent += cost or Decimal(0)
    return spent
