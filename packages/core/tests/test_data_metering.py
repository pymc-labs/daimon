"""The data store's meter: a balance gate, rate-table prices and usage rows."""

from __future__ import annotations

from decimal import Decimal

from daimon.core.ports.data_metering import SessionMeter
from daimon.core.stores import tenant_ledger, usage_events
from daimon.testing.factories import make_ledger_entry, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_embedding_spend_is_a_usage_row_and_a_debit_behind_a_balance_gate(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An embedding is billed at its rate-table price, and an empty balance stops more."""
    tenant = await make_tenant(db_session)
    meter = SessionMeter(db_session_factory)
    assert not await meter.allowed(tenant.id), "a tenant with nothing prepaid may not spend"
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("1"))
    assert await meter.allowed(tenant.id), "a funded tenant may"
    assert meter.priced("text-embedding-3-small"), "a model with a rate is priced"
    assert not meter.priced("not-a-model"), "a model without one cannot be billed"

    await meter.record(
        "data_embedding",
        tenant_id=tenant.id,
        platform_user_id="u1",
        model="text-embedding-3-small",
        units=1_000_000,
    )

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert [(r.model, r.input_tokens, r.output_tokens) for r in rows] == [
        ("text-embedding-3-small", 1_000_000, 0)
    ], "the spend is one usage row on the embedding model"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance == Decimal("0.98"), "a million tokens cost two cents, debited from the balance"
