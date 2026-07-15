"""Billing config + admission decision. Pure module + DB read.

Per `guideline:architecture` — `is_over_cap` is the single source of truth
for admission. Adapter wrappers call it before `anthropic.beta.sessions.create`
(D-01, D-02). No-cap-row short-circuits to False (D-06). Otherwise: True iff
sum(usage_events.cost since calendar-month-UTC) >= effective_cap.

Exceptions propagate per D-24 (fail-closed admission).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.core.stores import tenant_user_caps, usage_events
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class BillingError(Exception):
    """Raised by `load_billing_config()` when required env vars are missing."""


@dataclass(frozen=True)
class BillingConfig:
    """Stripe + checkout config. Loaded once at app boot.

    Per `guideline:architecture` "no module-level singletons" — `BillingConfig`
    is constructed by `load_billing_config()` and injected by callers
    (adapters/mcp/server.py:create_mcp_app). No global state.
    """

    secret_key: SecretStr
    webhook_secret: SecretStr
    prices: dict[int, str]  # cents amount (10, 25, 50, 100) -> Stripe price id
    success_url: str
    cancel_url: str


def load_billing_config() -> BillingConfig | None:
    """Read env vars; return None when Stripe vars are absent."""
    required = (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_10_USD",
        "STRIPE_PRICE_25_USD",
        "STRIPE_PRICE_50_USD",
        "STRIPE_PRICE_100_USD",
        "MCP_PUBLIC_URL",
    )
    values: dict[str, str] = {}
    missing: list[str] = []
    for key in required:
        v = os.environ.get(key)
        if v is None or v == "":
            missing.append(key)
        else:
            values[key] = v
    if missing:
        log.info("billing disabled", reason="no STRIPE_* env vars", missing=missing)
        return None

    public_url = values["MCP_PUBLIC_URL"]
    return BillingConfig(
        secret_key=SecretStr(values["STRIPE_SECRET_KEY"]),
        webhook_secret=SecretStr(values["STRIPE_WEBHOOK_SECRET"]),
        prices={
            10: values["STRIPE_PRICE_10_USD"],
            25: values["STRIPE_PRICE_25_USD"],
            50: values["STRIPE_PRICE_50_USD"],
            100: values["STRIPE_PRICE_100_USD"],
        },
        success_url=f"{public_url}/billing/success",
        cancel_url=f"{public_url}/billing/cancel",
    )


def _calendar_month_start_utc(now: datetime) -> datetime:
    """First day of the calendar month at 00:00 UTC. D-09. Pitfall 3."""
    return datetime(now.year, now.month, 1, tzinfo=UTC)


async def is_over_cap(
    *,
    billing_config: BillingConfig | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: str,
    now: datetime,
) -> bool:
    """Adapter-edge admission decision. D-02, D-06, D-08, D-09.

    Returns True iff the user has spent at-or-above their effective cap
    in the calendar-month UTC window containing ``now``. Returns False when
    billing_config is None (billing disabled). Exceptions propagate (D-24).

    ``now`` is injected by the caller (the adapter edge) rather than read from
    the clock here, per guideline:architecture — keeps the decision pure and
    testable without monkeypatching.
    """
    if billing_config is None:
        return False
    async with sessionmaker() as s:
        cap = await tenant_user_caps.get_effective_cap(
            s,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if cap is None:
            return False  # D-06 no row = uncapped
        period_start = _calendar_month_start_utc(now)
        spent = await usage_events.cost_for_user_in_tenant_since(
            s,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            since=period_start,
        )
    # Pitfall 8 — compare in Decimal to avoid float drift
    return Decimal(str(spent)) >= cap
