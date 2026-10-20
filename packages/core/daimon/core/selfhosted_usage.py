"""Azure token metering with explicit deployment/model pricing and durable repair.

Tariffs belong to the operator's region and purchase agreement. Neither an
Azure deployment name nor public OpenAI pricing establishes its cost. The
receipt keeps its model identity; a configured alias selects the underlying
model tariff without rewriting native usage or request ids.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType

from daimon.core.config import SelfhostedBillingSettings
from daimon.core.pricing import ProviderPrice
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def selfhosted_prices(
    settings: SelfhostedBillingSettings, *, on: date | None = None
) -> Mapping[tuple[str, str], ProviderPrice]:
    """Expose current explicit tariffs for deployment and underlying model receipts.

    Ambiguous aliases are refused rather than choosing a tariff by insertion
    order. Out-of-window entries stay absent. Recovery supplies the receipt
    date; admission defaults to the current UTC date.
    """
    prices: dict[tuple[str, str], ProviderPrice] = {}
    on = on or datetime.now(UTC).date()
    identities = {model: model for model in settings.models}
    for deployment, model in settings.deployments.items():
        if (
            not deployment
            or not model
            or (deployment in identities and identities[deployment] != model)
        ):
            raise ValueError("ambiguous selfhosted deployment/model pricing identity")
        identities[deployment] = model
    for identity, model in identities.items():
        rate = settings.models.get(model)
        if rate is None:
            continue
        price = ProviderPrice(
            provider="selfhosted",
            model=identity,
            checked_on=rate.checked_on,
            input=rate.input,
            output=rate.output,
            cache_read=rate.cache_read,
            valid_before=rate.valid_before,
        )
        if price.current_on(on):
            prices[("selfhosted", identity)] = price
    return MappingProxyType(prices)


def require_selfhosted_price(settings: SelfhostedBillingSettings, deployment: str) -> ProviderPrice:
    """Refuse an unmapped/unpriced deployment before starting paid model work."""
    prices = selfhosted_prices(settings)
    if deployment not in settings.deployments or ("selfhosted", deployment) not in prices:
        raise AdmissionDenied(reason="backend_unsupported")
    return prices[("selfhosted", deployment)]


async def record_selfhosted_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    binding_id: str,
    observation: UsageObservation,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    billing: SelfhostedBillingSettings,
    deployment: str,
    infrastructure_usd: Decimal | None = None,
    markup: Decimal = Decimal("1"),
    channel_id: str | None = None,
) -> bool:
    """Settle one request once, retaining unavailable usage for reconciliation.

    Bind deployment from admission, never event input. Receipt model may be
    that deployment or its configured underlying model. Missing/changed prices
    after admission remain pending. infrastructure_usd is measured per request;
    an explicit audited zero means the operator funds compute separately.
    Unknown/partial completeness cannot settle even with populated counters.
    Retry a pending snapshot unchanged or repair it with a higher revision of
    the same request id; neither durable replay nor signed correction rebills it.
    """
    if (
        observation.session.provider != "selfhosted"
        or observation.grain != "model_request"
        or observation.basis != "increment"
        or observation.covers
        or observation.thread_id is not None
    ):
        raise ValueError("selfhosted billing requires incremental model-request usage")
    underlying = billing.deployments.get(deployment)
    price = None
    if observation.model is not None:
        if observation.model.provider != "selfhosted":
            raise ScopeViolation(observation.id, "selfhosted receipt belongs to another provider")
        # Retain unexpected or newly unpriced receipts for operator repair;
        # never price an event using an unrelated admitted deployment's tariff.
        if underlying is not None and observation.model.id in (deployment, underlying):
            price = selfhosted_prices(billing, on=observation.observed_at.date()).get(
                ("selfhosted", observation.model.id)
            )
    return await record_provider_usage(
        sessionmaker=sessionmaker,
        binding_id=binding_id,
        observation=observation,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        provider_price=price,
        infrastructure_usd=infrastructure_usd,
        billing_grain="model_request",
        markup=markup,
        channel_id=channel_id,
    )
