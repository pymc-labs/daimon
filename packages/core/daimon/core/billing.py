"""Billing config + admission decision. Pure module + DB read.

Per `guideline:architecture` — `is_over_cap` is the single source of truth
for admission. Adapter wrappers call it before `anthropic.beta.sessions.create`
No-cap-row short-circuits to False. Otherwise: True iff
sum(usage_events.cost since calendar-month-UTC) >= effective_cap.

Exceptions propagate (fail-closed admission).
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from daimon.core.stores import tenant_ledger, tenant_user_caps, usage_events
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from stripe import StripeClient
    from stripe.params.checkout._session_create_params import SessionCreateParams

log = structlog.get_logger(__name__)


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


def month_start(now: datetime) -> datetime:
    """First day of the calendar month at 00:00 UTC: the cap period the panels report."""
    return datetime(now.year, now.month, 1, tzinfo=UTC)


async def is_over_cap(
    *,
    billing_config: BillingConfig | None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    tenant_id: uuid.UUID,
    user_id: str,
    now: datetime,
    session: AsyncSession | None = None,
) -> bool:
    """Adapter-edge admission decision.

    Returns True iff the user has spent at-or-above their effective cap
    in the calendar-month UTC window containing ``now``. Caps apply even when
    Stripe is not configured. Exceptions propagate.

    ``now`` is injected by the caller (the adapter edge) rather than read from
    the clock here, per guideline:architecture — keeps the decision pure and
    testable without monkeypatching.
    """

    async def check(s: AsyncSession) -> bool:
        cap = await tenant_user_caps.get_effective_cap(s, tenant_id=tenant_id, user_id=user_id)
        if cap is None:
            return False
        spent = await usage_events.cost_for_user_in_tenant_since(
            s, tenant_id=tenant_id, platform_user_id=user_id, since=month_start(now)
        )
        return Decimal(str(spent)) >= cap

    if session is not None:
        return await check(session)
    if sessionmaker is None:
        raise ValueError("is_over_cap needs a session or a sessionmaker")
    async with sessionmaker() as opened:
        return await check(opened)


class CheckoutAmountUnavailable(ValueError):
    """The requested top-up amount has no Stripe price configured."""


async def create_checkout_session(
    stripe_client: StripeClient,
    billing_config: BillingConfig,
    *,
    tenant_id: uuid.UUID,
    amount: int,
) -> str | None:
    """Start a one-time Stripe Checkout for `amount` whole dollars; the session's URL.

    `amount` must be one of the configured prices, else `CheckoutAmountUnavailable`.
    The credit lands on `tenant_id` when the payment webhook arrives, not here. The
    caller owns who may buy credit and which tenant it is for.
    """
    price_id = billing_config.prices.get(amount)
    if price_id is None:
        raise CheckoutAmountUnavailable(str(amount))
    params: SessionCreateParams = {
        "mode": "payment",  # one-time, NOT subscription
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": billing_config.success_url,
        "cancel_url": billing_config.cancel_url,
        "metadata": {
            "tenant_id": str(tenant_id),  # the credit target
        },
    }
    session = await stripe_client.v1.checkout.sessions.create_async(params)
    log.info(
        "stripe.checkout.session_created",
        tenant_id=str(tenant_id),
        amount=amount,
        session_id=session.id,
    )
    return session.url


def credit_note_slug(note: str) -> str:
    """The note as the lowercase words that go in a credit's key; ValueError when it has none."""
    if not note.strip():
        raise ValueError("note must not be empty")
    slug = re.sub(r"[_\W]+", "-", note.strip().lower()).strip("-")[:64].rstrip("-")
    if not slug:
        raise ValueError("note must contain letters or numbers")
    return slug


@dataclass(frozen=True)
class ManualCredit:
    """A manual credit: its idempotency key, request id and whether this call wrote it."""

    key: str
    request_id: str
    inserted: bool


async def manual_credit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    amount: Decimal,
    note: str,
    request_id: str | None = None,
    actor_account_id: uuid.UUID | None = None,
) -> ManualCredit:
    """Add `amount` USD of credit to the tenant, once per `request_id`.

    The key is derived from the tenant, the note, the amount and the request id, so
    repeating a request never credits twice. Raises ValueError for a non-positive amount
    or a note with no letters or numbers. The caller commits.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    note_slug = credit_note_slug(note)
    request_id = request_id or str(uuid.uuid4())
    key = f"manual:credit:{tenant_id}:{note_slug}:usd{amount:.2f}:{request_id}"
    inserted = await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=amount,
        reason="manual_credit",
        idempotency_key=key,
        actor_account_id=actor_account_id,
    )
    return ManualCredit(key=key, request_id=request_id, inserted=inserted)
