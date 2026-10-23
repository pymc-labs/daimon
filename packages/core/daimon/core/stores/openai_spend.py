"""What a tenant spent today on one OpenAI model, for the daily model cap.

OpenAI turns write no ledger debit (their infrastructure cost is unknown), so
today's spend is priced from the observed token usage at list price, plus the
reservations of fleet workers on that model that are still running. Counted in
provider dollars (no markup), from midnight UTC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.core._models import FleetWorker, UsageObservation
from daimon.core.pricing import ProviderPrice, minimum_cost_of, provider_cost_of
from mux.state.usage_ledger import AppliedUsage
from pydantic import ValidationError
from sqlalchemy import DateTime, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

_ACTIVE = ("pending", "starting", "running")
log = structlog.get_logger(__name__)


class SpendUnpriced(Exception):
    """Today's spend on this model can't be priced (no current tariff): fail closed."""


def utc_midnight(now: datetime) -> datetime:
    """The start of `now`'s UTC day, when every daily model cap resets."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def openai_spend_today(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    model: str,
    now: datetime,
    rates: ProviderPrice | None,
    markup: Decimal,
) -> Decimal:
    """Observed usage today on `model` at list price, plus its running workers' holds.

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
            log.warning("openai_spend.usage_row_skipped", tenant_id=str(tenant_id), model=model)
            continue
        spent += cost or Decimal(0)
    # A running worker's reservation is customer dollars (with markup). Counted in
    # full, even where some of its usage is already observed: never under-counts.
    held = await session.scalar(
        select(func.coalesce(func.sum(FleetWorker.reserved_usd), 0)).where(
            FleetWorker.tenant_id == tenant_id,
            FleetWorker.backend == provider,
            FleetWorker.model == model,
            FleetWorker.status.in_(_ACTIVE),
        )
    )
    return spent + Decimal(held or 0) / (markup if markup > 0 else Decimal(1))
