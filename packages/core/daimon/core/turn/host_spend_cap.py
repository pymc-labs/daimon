"""The host-side spend cap of a turn, for a profile with no native session spend control.

OpenAI answers `spend_control` with HTTP 400 on this project, so the limit a row
carries (`spend_limit_usd_cents`) is enforced here: the turn's priced usage is polled
while it streams and the turn is cancelled once it passes the limit.

Usage is priced at the tariff without cache writes: the meter reports none, so such a
turn counts as writing nothing to the cache (`pricing.unreported_cache_writes`, an
estimate that never exceeds what the counts prove). Container time is not counted; the
cap bounds tokens.

Past a tariff's priced input limit with no sourced long-context rate (gpt-6-luna), the
usage is counted at the base rate: a lower bound, so the cap still fires.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from decimal import Decimal

import structlog
from daimon.core.pricing import ProviderPrice, provider_cost_of
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.usage import UsageObservation

__all__ = ["HOST_SPEND_CAP_NOTICE", "SpendGuard", "priced_cents"]

log = structlog.get_logger(__name__)

HOST_SPEND_CAP_NOTICE = (
    "Your last reply was stopped because it passed the spend limit set for this agent."
)
POLL_INTERVAL_S = 5.0


def priced_cents(
    observations: Iterable[UsageObservation], price: ProviderPrice, provider: Provider = "openai"
) -> Decimal | None:
    """The usage so far in US cents, newest revision of each observation; None if unpriceable.

    A running snapshot is a lower bound, so a partial one counts as measured: the cap only
    needs what has provably been spent. An unknown one cannot be priced. The meter
    names no model: `provider` and the price's model stand for the admitted one. Usage
    past the priced input limit with no long-context rate counts at the base rate.
    """
    floor = (
        replace(price, max_priced_input_tokens=None, long_context_multipliers=None)
        if price.max_priced_input_tokens is not None and price.long_context_multipliers is None
        else None
    )
    latest: dict[str, UsageObservation] = {}
    for each in observations:
        held = latest.get(each.id)
        if held is None or each.revision > held.revision:
            latest[each.id] = each
    total = Decimal(0)
    for each in latest.values():
        if each.completeness == "unknown":
            return None
        # The meter names no model; the admitted one is the model of the turn.
        counted = each.model_copy(
            update={
                "model": ModelRef(provider=provider, id=price.model),
                **({"completeness": "measured"} if each.completeness == "partial" else {}),
            }
        )
        cost = provider_cost_of(counted, price, infrastructure_usd=Decimal(0))
        if cost is None and floor is not None:
            cost = provider_cost_of(counted, floor, infrastructure_usd=Decimal(0))
            if cost is not None:
                log.warning("turn.spend_priced_at_base_rate", model=price.model)
        if cost is None:
            return None
        total += cost
    return total * 100


class SpendGuard:
    """Says when a turn's priced usage passes `limit_cents`, asking the meter at most every
    `interval_s`. A meter that cannot be read or priced keeps the last known total and
    warns; it never ends a turn on its own."""

    def __init__(
        self,
        *,
        limit_cents: int,
        price: ProviderPrice,
        notify: Callable[[], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        if limit_cents <= 0:
            raise ValueError("a spend limit must be positive")
        self._limit, self._price, self.notify = Decimal(limit_cents), price, notify
        self._clock, self._interval = clock, interval_s
        self._next = float("-inf")
        self._spent, self._misses = Decimal(0), 0

    async def exceeded(self, read: Callable[[], Awaitable[tuple[UsageObservation, ...]]]) -> bool:
        now = self._clock()
        if now < self._next:
            return False
        self._next = now + self._interval
        spent = priced_cents(await read(), self._price)
        if spent is None:
            return self._missed("unpriced")
        self._spent, self._misses = max(self._spent, spent), 0
        return self._spent > self._limit

    def unread(self, error: Exception) -> bool:
        """The meter could not be read: keep the last known total."""
        return self._missed(type(error).__name__)

    def _missed(self, reason: str) -> bool:
        self._misses += 1
        log.warning(
            "turn.host_spend_cap_missed",
            reason=reason,
            misses=self._misses,
            spent_cents=str(self._spent),
        )
        return self._spent > self._limit
