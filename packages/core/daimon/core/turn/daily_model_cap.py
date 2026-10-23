"""The daily spend cap per tenant per native model (OpenAI, Gemini).

Native OpenAI and Gemini usage reaches no ledger debit today, so the balance,
user and channel gates can't see it. This cap is the guard: checked before any
native call of an agent turn or a fleet worker, from today's (UTC) observed usage
at list price plus running workers' reservations (`stores.model_spend`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from daimon.core.pricing import ProviderPrice
from daimon.core.turn.errors import AdmissionDenied

if TYPE_CHECKING:
    from collections.abc import Mapping

    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.contracts.ids import ModelRef
    from mux.drivers.gemini import FlashAccounting, FlashAttempt

log = structlog.get_logger(__name__)


async def check_daily_model_cap(
    request: ProviderPreparationRequest,
    *,
    provider: str,
    model: str | None,
    price: ProviderPrice | None,
    cap: Decimal | None,
) -> None:
    """Refuse once the tenant's spend today on `model` reaches `cap`.

    A worker's own reservation already counts toward today's spend, so it may use
    the cap up exactly; an agent turn starts only while some of it remains. With no
    current tariff the spend can't be known, so the turn is refused (fail closed).
    """
    from daimon.core.stores.model_spend import SpendUnpriced, model_spend_today

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
                markup=request.deps.markup,
            )
    except SpendUnpriced:
        log.warning("turn.daily_model_cap_unpriced", tenant_id=str(request.tenant_id), model=model)
        raise AdmissionDenied(reason="provider_price_unavailable", provider=model) from None
    if spent > cap if request.worker is not None else spent >= cap:
        log.warning(
            "turn.daily_model_cap_reached",
            tenant_id=str(request.tenant_id),
            model=model,
            cap_usd=str(cap),
        )
        raise AdmissionDenied(reason="provider_daily_cap_reached", provider=model)


class CappedFlashAccounting:
    """Gemini's per-attempt hold ledger, checking the daily model cap for each attempt.

    The Flash chain may fall back to another model on a 503 (`FLASH_MODELS`), so the
    cap is checked for the model each attempt names, before its hold and its create.
    A fallback without a current tariff refuses rather than running uncounted.
    """

    def __init__(
        self,
        inner: FlashAccounting,
        request: ProviderPreparationRequest,
        prices: Mapping[str, ProviderPrice],
        cap: Decimal | None,
    ) -> None:
        self._inner, self._request, self._prices, self._cap = inner, request, prices, cap

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        await check_daily_model_cap(
            self._request,
            provider=model.provider,
            model=model.id,
            price=self._prices.get(model.id),
            cap=self._cap,
        )
        await self._inner.before_attempt(model=model, attempt=attempt)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        await self._inner.after_attempt(receipt=receipt)

    def __getattr__(self, name: str) -> object:
        # Settlement and the rest of the ledger are the inner accounting's own.
        return getattr(self._inner, name)
