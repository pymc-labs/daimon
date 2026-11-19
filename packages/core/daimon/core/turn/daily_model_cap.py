"""The daily spend cap per tenant per native model (OpenAI, Gemini), and the
deployment's daily ceiling per provider across every tenant.

The balance, allowance and channel gates count settled debits, which can trail a
turn and miss usage still pending. These caps count today's (UTC) observed usage
at list price (`stores.model_spend`), and are checked before any native call of an
agent turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from daimon.core.pricing import ProviderPrice
from daimon.core.turn.errors import AdmissionDenied

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.contracts.ids import ModelRef
    from mux.drivers.gemini import FlashAccounting, FlashAttempt
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


async def check_daily_model_cap(
    request: ProviderPreparationRequest,
    *,
    provider: str,
    model: str | None,
    price: ProviderPrice | None,
    cap: Decimal | None,
) -> None:
    """Refuse once the tenant's spend today on `model` reaches `cap`, or the lower cap a
    workspace or agent chose (`daily_model_cap`).

    An agent turn starts only while some of the cap remains. With no current
    tariff the spend can't be known, so the turn is refused (fail closed).
    """
    from daimon.core.stores.model_spend import SpendUnpriced, model_spend_today

    if request.deps.provider_ceiling is not None:
        await request.deps.provider_ceiling.check(
            request.deps.sessionmaker, provider=provider, now=request.now()
        )
    chosen = request.admission.resolved("daily_model_cap")
    if chosen is not None and chosen.tier != "default":
        # A tier can only lower the operator's provider cap.
        cap = Decimal(chosen.value) if cap is None else min(cap, Decimal(chosen.value))
    if cap is None or model is None:
        return
    try:
        async with request.deps.sessionmaker() as session:
            spent = await model_spend_today(
                session,
                tenant_id=request.tenant_id,
                provider=provider,
                model=model,
                now=request.now(),
                rates=price,
            )
    except SpendUnpriced:
        log.warning("turn.daily_model_cap_unpriced", tenant_id=str(request.tenant_id), model=model)
        raise AdmissionDenied(reason="provider_price_unavailable", provider=model) from None
    if spent >= cap:
        log.warning(
            "turn.daily_model_cap_reached",
            tenant_id=str(request.tenant_id),
            model=model,
            cap_usd=str(cap),
        )
        raise AdmissionDenied(reason="provider_daily_cap_reached", provider=model)


@dataclass
class ProviderCeiling:
    """The deployment's daily spend ceiling per provider, summed across every tenant.

    Today's spend is re-read at most every `ttl_s` per provider, so a turn start costs
    one scan of the day's usage a minute, not one per turn; turns admitted within that
    window may pass the ceiling by what they spend.
    """

    limits_usd: Mapping[str, Decimal]
    ttl_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _read: dict[str, tuple[float, Decimal]] = field(
        default_factory=dict[str, tuple[float, Decimal]], init=False, repr=False
    )

    async def check(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, provider: str, now: datetime
    ) -> None:
        """Refuse once every tenant's spend today on `provider` reaches its ceiling."""
        from daimon.core.stores.model_spend import provider_spend_today

        limit = self.limits_usd.get(provider)
        if limit is None:
            return
        read = self._read.get(provider)
        if read is None or self.clock() >= read[0]:
            async with sessionmaker() as session:
                spent = await provider_spend_today(session, provider=provider, now=now)
            read = self._read[provider] = (self.clock() + self.ttl_s, spent)
        if read[1] >= limit:
            log.warning(
                "turn.provider_daily_ceiling_reached", provider=provider, ceiling_usd=str(limit)
            )
            raise AdmissionDenied(reason="provider_daily_ceiling_reached", provider=provider)


class CappedFlashAccounting:
    """Gemini's per-attempt hold ledger, checking the daily model cap as a turn starts.

    The Flash chain may fall back to another model on a 503 (`FLASH_MODELS`), so the
    cap is checked for the model each attempt names, before its hold and its create,
    until the turn's first request is accepted. Later requests (tool-result
    continuations) are never refused halfway through an answer. A model without a
    current tariff never runs on any request, first or later: its spend could be
    neither billed nor capped. The turn's own model is refused plainly; a fallback
    ends the turn with the primary's 503.
    """

    def __init__(
        self,
        inner: FlashAccounting,
        request: ProviderPreparationRequest,
        prices: Mapping[str, ProviderPrice],
        cap: Decimal | None,
    ) -> None:
        self._inner, self._request, self._prices, self._cap = inner, request, prices, cap
        self._started = False

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        price = self._prices.get(model.id)
        if price is None or not price.current_on(self._request.now().date()):
            log.warning("turn.unpriced_model_refused", model=model.id, attempt=attempt)
            if attempt == 1 and not self._started:
                raise AdmissionDenied(reason="provider_price_unavailable", provider=model.id)
            from mux.contracts.errors import ProviderError

            raise ProviderError("overloaded", retryable=True, native_code="503")
        if not self._started:
            await check_daily_model_cap(
                self._request, provider=model.provider, model=model.id, price=price, cap=self._cap
            )
        await self._inner.before_attempt(model=model, attempt=attempt)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        if receipt.outcome == "accepted":
            self._started = True
        await self._inner.after_attempt(receipt=receipt)

    # Explicit, never forwarded: `HoldSettlement` is a runtime-checkable Protocol,
    # which does not see methods a `__getattr__` would forward.
    async def settle(self, *, interaction_id: str, model: str, actual_usd: Decimal) -> bool:
        from daimon.core.turn.gemini_state import HoldSettlement

        if not isinstance(self._inner, HoldSettlement):
            return False
        return await self._inner.settle(
            interaction_id=interaction_id, model=model, actual_usd=actual_usd
        )

    async def release_unbilled(self, *, interaction_id: str) -> bool:
        release = getattr(self._inner, "release_unbilled", None)
        if release is None:
            return False
        return bool(await release(interaction_id=interaction_id))
