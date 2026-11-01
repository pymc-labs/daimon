"""Checkout creation and manual credit, the billing helpers the web and the MCP route share."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import billing
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

_CONFIG = billing.BillingConfig(
    secret_key=SecretStr("sk_test"),
    webhook_secret=SecretStr("whsec_test"),
    prices={10: "price_10", 25: "price_25"},
    success_url="https://example.test/success",
    cancel_url="https://example.test/cancel",
)


def _stripe(created: list[dict[str, Any]]) -> Any:
    async def create_async(params: dict[str, Any]) -> SimpleNamespace:
        created.append(params)
        return SimpleNamespace(id="cs_1", url="https://checkout.example.test/cs_1")

    sessions = SimpleNamespace(create_async=create_async)
    return SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=sessions)))


async def test_checkout_session_is_one_time_payment_for_the_configured_price() -> None:
    created: list[dict[str, Any]] = []
    tenant_id = uuid.uuid4()
    url = await billing.create_checkout_session(
        cast(Any, _stripe(created)), _CONFIG, tenant_id=tenant_id, amount=25
    )
    assert url == "https://checkout.example.test/cs_1", "the session's URL is returned"
    assert created == [
        {
            "mode": "payment",
            "line_items": [{"price": "price_25", "quantity": 1}],
            "success_url": "https://example.test/success",
            "cancel_url": "https://example.test/cancel",
            "metadata": {"tenant_id": str(tenant_id)},
        }
    ], "one payment session names the price and the tenant to credit"


async def test_checkout_refuses_an_amount_with_no_price() -> None:
    created: list[dict[str, Any]] = []
    with pytest.raises(billing.CheckoutAmountUnavailable):
        await billing.create_checkout_session(
            cast(Any, _stripe(created)), _CONFIG, tenant_id=uuid.uuid4(), amount=7
        )
    assert created == [], "no session is created for an unpriced amount"


async def test_manual_credit_is_idempotent_and_records_who_made_it(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    first = await billing.manual_credit(
        db_session,
        tenant_id=tenant.id,
        amount=Decimal("12.50"),
        note=" Support goodwill ",
        request_id="req-1",
        actor_account_id=account.id,
    )
    again = await billing.manual_credit(
        db_session,
        tenant_id=tenant.id,
        amount=Decimal("12.50"),
        note="support goodwill",
        request_id="req-1",
    )
    assert first.inserted and not again.inserted, "the same request credits once"
    assert first.key == f"manual:credit:{tenant.id}:support-goodwill:usd12.50:req-1", (
        "the key keeps the scheme the CLI always used"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("12.50")
    (row,) = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    assert row.reason == "manual_credit", "it is a manual credit row"
    assert row.actor_account_id == account.id, "the row names who made it"


@pytest.mark.parametrize(
    ("amount", "note"),
    [(Decimal(0), "ok"), (Decimal(-1), "ok"), (Decimal(1), "  "), (Decimal(1), "!!!")],
)
async def test_manual_credit_refuses_a_bad_amount_or_note(
    db_session: AsyncSession, amount: Decimal, note: str
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(ValueError):
        await billing.manual_credit(db_session, tenant_id=tenant.id, amount=amount, note=note)
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal(0), (
        "nothing is credited"
    )
