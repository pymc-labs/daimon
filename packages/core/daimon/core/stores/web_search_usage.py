"""Web searches counted against a provider's free monthly allowance.

The allowance belongs to the provider account, so every tenant on it shares one
row per pool and month. Callers hold the usage observation's lock, so a replayed
or retried revision never claims twice.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from daimon.core._models import WebSearchAllowance
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# Google bills by Pacific-time month; the providers publish no other zone.
_BILLING_ZONE = ZoneInfo("America/Los_Angeles")


def period_of(at: datetime) -> date:
    """The first day of the provider billing month `at` falls in."""
    return at.astimezone(_BILLING_ZONE).date().replace(day=1)


async def claim(
    session: AsyncSession,
    *,
    provider: str,
    account_scope_id: str,
    pool: str,
    at: datetime,
    searches: int,
    free_monthly: int,
) -> int:
    """Count `searches` more this month; return how many of them were free.

    One atomic upsert: a concurrent claim waits on the row lock until this
    transaction ends, then counts on top, so two turns crossing the limit
    never both get the same free search.
    """
    if searches < 0:
        raise ValueError("web searches cannot be negative")
    if not searches:
        return 0
    insert = pg_insert(WebSearchAllowance).values(
        provider=provider,
        account_scope_id=account_scope_id,
        pool=pool,
        period=period_of(at),
        used=searches,
    )
    used = await session.scalar(
        insert.on_conflict_do_update(
            index_elements=["provider", "account_scope_id", "pool", "period"],
            set_={"used": WebSearchAllowance.used + searches},
        ).returning(WebSearchAllowance.used)
    )
    assert used is not None
    return max(0, min(searches, free_monthly - (used - searches)))
