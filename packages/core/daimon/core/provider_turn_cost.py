"""What a finished GPT or Gemini turn debited, for the footer of every chat platform.

Prices each observation as the usage outbox settlement does (the same tariff,
infrastructure and markup rounding), so the footer's "used" matches the balance.
None while the cost is not known: usage not reported yet, partial, an unpriced
model, a profile whose infrastructure cannot settle, or searches whose free
allowance is only claimed at settlement.

A GPT turn's usage can arrive after the turn ended (`openai_late_usage`). The
recorder hands each recorded revision, priced with the turn's own tariff and
infrastructure, to `LATE_TURN_COSTS`: the footer takes its debit when it renders,
or waits for it and edits it in.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

import structlog
from daimon.core.infrastructure_cost import ZERO_INFRASTRUCTURE_PROFILES, known_infrastructure_usd
from daimon.core.pricing import (
    ProviderPrice,
    provider_cost_of,
    provider_prices,
    provider_usage_tokens,
)
from daimon.core.turn.openai_late_usage import DELAYS_S
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.state import UsageTotals
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.usage import UsageObservation
from mux.profiles import get_profile

log = structlog.get_logger(__name__)

#: Edits a debit priced after the turn ended into the footer the turn posted; True once it did.
type ShowDebit = Callable[[float], Awaitable[bool]]


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
        provider_prices(profile, on=observation.observed_at.date()).get(
            (provider, observation.model.id)
        ),
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


@dataclass(eq=False)
class _Footer:
    show: ShowDebit
    context: contextvars.Context
    place: tuple[str, str, str | None] | None
    expires: float
    shown: Decimal | None
    latest: Decimal | None = None
    final: bool = False
    #: The one edit in flight; it shows `latest` until that one landed.
    task: asyncio.Task[None] | None = None


def _place(outcome: TurnObservation | None) -> tuple[str, str, str | None] | None:
    return (
        None if outcome is None else (str(outcome.tenant_id), outcome.platform, outcome.thread_id)
    )


class LateTurnCosts:
    """Debits settled for a turn's usage, and footers waiting for them, in this process.

    Best effort: a footer keeps the cost it posted with once a later turn starts in
    its thread (`release`: that turn may edit the message), once its re-reads are
    over (`max_age_s`), or once `max_waiting` newer footers pushed it out. A debit
    another process settles never reaches this one. A refused or failed edit is
    retried after each of `retry_s`. Nothing here raises into, or waits inside, the
    settlement that calls it.
    """

    def __init__(
        self,
        *,
        max_waiting: int = 256,
        max_age_s: float = sum(DELAYS_S) + 60.0,
        retry_s: Sequence[float] = (1.0, 4.0, 16.0),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_waiting, self._max_age_s, self._retry_s = max_waiting, max_age_s, retry_s
        self._clock = clock
        self._settled: OrderedDict[tuple[str, str], tuple[Decimal, bool]] = OrderedDict()
        self._waiting: OrderedDict[tuple[str, str], _Footer] = OrderedDict()
        self._shows: set[asyncio.Task[None]] = set()

    def turn_debit(self, totals: UsageTotals, *, markup: Decimal, show: ShowDebit) -> float | None:
        """The debit a finished turn's footer shows; `show` gets one settled after this.

        Only a turn of one observation can take a later debit: a GPT turn's own usage.
        `show` returns whether it edited the footer in.
        """
        guessed = provider_turn_debit(totals, markup=markup)
        if not isinstance(totals, ProviderUsageTotals) or len(totals.selected) != 1:
            return guessed
        (observation,) = totals.selected
        key = (observation.session.id, observation.id)
        debit, final = self._settled.get(key, (None, observation.final))
        if not final:
            now = self._clock()
            while self._waiting and next(iter(self._waiting.values())).expires <= now:
                self._waiting.popitem(last=False)
            # The footer renders in the turn's context (its backend line), not the re-read's.
            self._waiting[key] = _Footer(
                show,
                contextvars.copy_context(),
                _place(current_outcome.get()),
                now + self._max_age_s,
                debit,
            )
            self._bound(self._waiting, key)
        return guessed if debit is None else float(debit)

    def release(self, tenant_id: object, platform: str, thread_id: str | None) -> None:
        """A turn starts in this thread: earlier footers there take no later debit."""
        place = (str(tenant_id), platform, thread_id)
        for key in [key for key, footer in self._waiting.items() if footer.place == place]:
            del self._waiting[key]

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
        """Hand a recorded revision's debit to the footer waiting on it; never raises or waits."""
        key = (observation.session.id, observation.id)
        try:
            debit = observation_debit(
                observation, price, infrastructure_usd=infrastructure_usd, markup=markup
            )
        except Exception:  # the footer is cosmetic: the settlement that called this goes on
            log.warning("late_turn_cost.price_failed", key=":".join(key), exc_info=True)
            return
        footer = self._waiting.get(key)
        if footer is not None and footer.expires <= self._clock():
            del self._waiting[key]
            return
        if footer is None:
            if debit is not None:  # settled before the footer rendered: it takes this one
                self._settled[key] = (debit, observation.final)
                self._bound(self._settled, key)
            return
        if debit is None:
            if observation.final:
                del self._waiting[key]
            return
        footer.latest, footer.final = debit, observation.final
        if footer.task is not None and not footer.task.done():
            return  # one edit per footer: the one in flight shows the newest debit next
        if debit == footer.shown:
            if observation.final:
                del self._waiting[key]
            return
        footer.task = asyncio.get_running_loop().create_task(
            self._show(key, footer), context=footer.context
        )
        self._shows.add(footer.task)
        footer.task.add_done_callback(self._shows.discard)

    async def wait(self) -> None:
        """Until every footer edit started so far has finished."""
        while self._shows:
            await asyncio.gather(*self._shows, return_exceptions=True)

    async def _show(self, key: tuple[str, str], footer: _Footer) -> None:
        delays, attempt, trying = (0.0, *self._retry_s), 0, footer.latest
        while attempt < len(delays):
            await asyncio.sleep(delays[attempt])
            debit = footer.latest
            if self._waiting.get(key) is not footer or debit is None:
                return  # released or expired
            if debit == footer.shown:
                if footer.final:
                    del self._waiting[key]
                return
            try:
                applied = await footer.show(float(debit))
            except Exception:  # a footer edit is cosmetic: a failed one is retried, then dropped
                log.warning("late_turn_cost.show_failed", key=":".join(key), exc_info=True)
                applied = False
            if applied:
                footer.shown = debit
            attempt = 0 if applied else attempt + 1
            if footer.latest != trying:  # settled meanwhile: the newest debit gets its own tries
                attempt, trying = 0, footer.latest


#: The process's waiting footers, shared by every chat platform's lifecycle.
LATE_TURN_COSTS = LateTurnCosts()
