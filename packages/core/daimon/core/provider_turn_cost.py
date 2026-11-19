"""What a finished GPT or Gemini turn debited, for the footer of every chat platform.

Prices each observation as the usage outbox settlement does (the same tariff,
infrastructure and markup rounding), so the footer's "used" matches the balance.
None while the cost is not known: usage not reported yet, partial, an unpriced
model, a profile whose infrastructure cannot settle, or searches whose free
allowance is only claimed at settlement.

A GPT turn's usage can arrive after the turn ended (`openai_late_usage`). The
recorder hands each settled debit, priced with the turn's own tariff and
infrastructure, to `LATE_TURN_COSTS`: the footer takes it when it renders, or waits
for it and edits it in.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from decimal import Decimal, localcontext

import structlog
from daimon.core.infrastructure_cost import ZERO_INFRASTRUCTURE_PROFILES, known_infrastructure_usd
from daimon.core.pricing import (
    ProviderPrice,
    provider_cost_of,
    provider_prices,
    provider_usage_tokens,
)
from daimon.core.turn.state import UsageTotals
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.usage import UsageObservation
from mux.profiles import get_profile

log = structlog.get_logger(__name__)

#: Edits a debit priced after the turn ended into the footer the turn posted.
type ShowDebit = Callable[[float], Awaitable[None]]


def observation_debit(
    observation: UsageObservation,
    price: ProviderPrice | None,
    *,
    infrastructure_usd: Decimal | None,
    markup: Decimal,
) -> Decimal | None:
    """One observation's debit, markup included, as settlement rounds it; None if not known."""
    if price is None:
        return None
    if observation.web_searches and (
        (price.web_search is not None and price.web_search.free_monthly)
        or provider_usage_tokens(observation, price) is None
    ):
        return None
    cost = provider_cost_of(observation, price, infrastructure_usd=infrastructure_usd)
    if cost is None:
        return None
    with localcontext() as context:
        context.prec = 80
        return (cost * markup).quantize(Decimal("0.000001"))


def _guessed_debit(observation: UsageObservation, markup: Decimal) -> Decimal | None:
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
    return observation_debit(
        observation,
        provider_prices(profile).get((provider, observation.model.id)),
        infrastructure_usd=known_infrastructure_usd(profile),
        markup=markup,
    )


def provider_turn_debit(totals: UsageTotals, *, markup: Decimal) -> float | None:
    """The turn's debit in USD, markup included; None when it is not known."""
    if not isinstance(totals, ProviderUsageTotals) or not totals.selected:
        return None
    debit = Decimal(0)
    for observation in totals.selected:
        cost = _guessed_debit(observation, markup)
        if cost is None:
            return None
        debit += cost
    return float(debit)


class LateTurnCosts:
    """Debits settled for a turn's usage, and footers waiting for them, in this process.

    Best effort: a footer whose usage another process re-reads (the scheduler's
    `DurableRereads`), or that `max_waiting` newer turns pushed out, keeps no
    cost. Nothing here raises into, or waits inside, the settlement that calls it.
    """

    def __init__(self, *, max_waiting: int = 256) -> None:
        self._max_waiting = max_waiting
        self._settled: OrderedDict[tuple[str, str], tuple[Decimal, bool]] = OrderedDict()
        self._waiting: OrderedDict[
            tuple[str, str], tuple[ShowDebit, contextvars.Context, Decimal | None]
        ] = OrderedDict()
        self._shows: set[asyncio.Task[None]] = set()

    def turn_debit(self, totals: UsageTotals, *, markup: Decimal, show: ShowDebit) -> float | None:
        """The debit a finished turn's footer shows; `show` gets one settled after this.

        Only a turn of one observation can take a later debit: a GPT turn's own usage.
        """
        guessed = provider_turn_debit(totals, markup=markup)
        if not isinstance(totals, ProviderUsageTotals) or len(totals.selected) != 1:
            return guessed
        (observation,) = totals.selected
        key = (observation.session.id, observation.id)
        debit, final = self._settled.get(key, (None, observation.final))
        if not final:
            # The footer renders in the turn's context (its backend line), not the re-read's.
            self._waiting[key] = (show, contextvars.copy_context(), debit)
            self._bound(self._waiting, key)
        return guessed if debit is None else float(debit)

    def _bound[V](self, entries: OrderedDict[tuple[str, str], V], key: tuple[str, str]) -> None:
        entries.move_to_end(key)
        while len(entries) > self._max_waiting:
            entries.popitem(last=False)

    def settle(
        self,
        observation: UsageObservation,
        *,
        price: ProviderPrice | None,
        infrastructure_usd: Decimal | None,
        markup: Decimal,
    ) -> None:
        """Hand a settled revision's debit to the footer waiting on it; never raises or waits."""
        key = (observation.session.id, observation.id)
        try:
            debit = observation_debit(
                observation, price, infrastructure_usd=infrastructure_usd, markup=markup
            )
        except Exception:  # the footer is cosmetic: the settlement that called this goes on
            log.warning("late_turn_cost.price_failed", key=":".join(key), exc_info=True)
            return
        if (waiting := self._waiting.get(key)) is None:
            if debit is not None:  # settled before the footer rendered: it takes this one
                self._settled[key] = (debit, observation.final)
                self._bound(self._settled, key)
            return
        show, context, shown = waiting
        if observation.final:
            del self._waiting[key]
        elif debit is not None:
            self._waiting[key] = (show, context, debit)
        if debit is None or debit == shown:
            return
        task = asyncio.get_running_loop().create_task(self._show(key, show, debit), context=context)
        self._shows.add(task)
        task.add_done_callback(self._shows.discard)

    async def wait(self) -> None:
        """Until every footer edit started so far has finished."""
        while self._shows:
            await asyncio.gather(*self._shows, return_exceptions=True)

    async def _show(self, key: tuple[str, str], show: ShowDebit, debit: Decimal) -> None:
        try:
            await show(float(debit))
        except Exception:  # a footer edit is cosmetic: a refused or failed one is only logged
            log.warning("late_turn_cost.show_failed", key=":".join(key), exc_info=True)


#: The process's waiting footers, shared by every chat platform's lifecycle.
LATE_TURN_COSTS = LateTurnCosts()
