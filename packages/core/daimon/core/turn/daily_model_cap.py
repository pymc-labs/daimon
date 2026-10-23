"""The daily spend cap per tenant per native model (OpenAI, Gemini).

Native OpenAI and Gemini usage reaches no ledger debit today, so the balance,
user and channel gates can't see it. This cap is the guard: checked before any
native call of an agent turn or a fleet worker, from today's (UTC) observed usage
at list price plus running workers' reservations (`stores.openai_spend`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from daimon.core.pricing import ProviderPrice
from daimon.core.turn.errors import AdmissionDenied

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest

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
    from daimon.core.stores.openai_spend import SpendUnpriced, openai_spend_today

    if cap is None or model is None:
        return
    try:
        async with request.deps.sessionmaker() as session:
            spent = await openai_spend_today(
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
        raise AdmissionDenied(reason="provider_daily_cap_reached", provider=model) from None
    if spent > cap if request.worker is not None else spent >= cap:
        log.warning(
            "turn.daily_model_cap_reached",
            tenant_id=str(request.tenant_id),
            model=model,
            cap_usd=str(cap),
        )
        raise AdmissionDenied(reason="provider_daily_cap_reached", provider=model)
