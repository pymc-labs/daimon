"""Per-tenant balance gate + debit math.

Admission: balance > 0. Composed with is_over_cap by the caller —
final admit = balance_ok AND NOT over_cap. The balance gate is INDEPENDENT of
Stripe config (RESEARCH Pitfall 4 / OQ#1): a trial-credit-only guild with no
STRIPE_* env must still turn. Gate on tenant_id presence + the ledger.

Per `guideline:architecture`: exceptions propagate (fail-closed admission).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from daimon.core.stores import tenant_ledger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def debit_amount(cost: float | None, *, markup: Decimal) -> Decimal:
    """Convert a float cost to Decimal and apply markup.

    Pitfall 6: cost_of returns float|None. Convert with Decimal(str(...)) before
    multiplying to avoid float drift. None cost -> Decimal("0").
    Returns Decimal quantized to 6 decimal places (sub-cent turn debits).
    """
    cost_decimal = Decimal(str(cost)) if cost is not None else Decimal("0")
    return (cost_decimal * markup).quantize(Decimal("0.000001"))


async def is_over_balance(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None,
) -> bool:
    """True iff the tenant balance is depleted (<= 0).

    tenant_id=None -> False (DM exemption — mirrors the cap DM exemption).
    Stripe-independent: no Stripe env required for this check (Pitfall 4 / OQ#1).
    Exceptions propagate (fail-closed admission).
    """
    if tenant_id is None:
        return False  # DMs have no tenant — mirror the cap DM exemption
    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant_id)
    return balance <= Decimal("0")
