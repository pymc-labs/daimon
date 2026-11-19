"""What a finished GPT or Gemini turn debited, for the footer of every chat platform.

Prices each observation as the usage outbox settlement does (the same tariff,
infrastructure and markup rounding), so the footer's "used" matches the balance.
None while the cost is not known: usage not reported yet, partial, an unpriced
model, a profile whose infrastructure cannot settle, or searches whose free
allowance is only claimed at settlement.
"""

from decimal import Decimal, localcontext

from daimon.core.infrastructure_cost import ZERO_INFRASTRUCTURE_PROFILES, known_infrastructure_usd
from daimon.core.pricing import provider_cost_of, provider_prices, provider_usage_tokens
from daimon.core.turn.state import UsageTotals
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.usage import UsageObservation
from mux.profiles import get_profile


def _observation_cost(observation: UsageObservation) -> Decimal | None:
    provider = observation.session.provider
    profile = next(
        (
            id_
            for id_ in sorted(ZERO_INFRASTRUCTURE_PROFILES)
            if get_profile(id_).provider == provider
        ),
        None,
    )
    if profile is None or observation.model is None:
        return None
    price = provider_prices(profile).get((provider, observation.model.id))
    if price is None:
        return None
    if observation.web_searches and (
        (price.web_search is not None and price.web_search.free_monthly)
        or provider_usage_tokens(observation, price) is None
    ):
        return None
    return provider_cost_of(
        observation, price, infrastructure_usd=known_infrastructure_usd(profile)
    )


def provider_turn_debit(totals: UsageTotals, *, markup: Decimal) -> float | None:
    """The turn's debit in USD, markup included; None when it is not known."""
    if not isinstance(totals, ProviderUsageTotals) or not totals.selected:
        return None
    debit = Decimal(0)
    with localcontext() as context:
        context.prec = 80
        for observation in totals.selected:
            cost = _observation_cost(observation)
            if cost is None:
                return None
            debit += (cost * markup).quantize(Decimal("0.000001"))
    return float(debit)
