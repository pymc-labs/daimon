"""Pending ordinary provider usage settles exactly once when its cost is known."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core import usage_outbox_drain
from daimon.core.stores import thread_sessions, usage_events
from daimon.core.usage_outbox_drain import drain_pending_usage
from sqlalchemy import text

from .native_usage_support import pending_usage, seed_binding
from .native_usage_support import sm as sm


@pytest.fixture(autouse=True)
def fresh_drain_cursor(monkeypatch):
    monkeypatch.setattr(usage_outbox_drain, "_cursor", None)


async def _pending(sm) -> int:
    async with sm() as db:
        return await db.scalar(
            text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NULL")
        )


async def _debited(sm) -> Decimal:
    async with sm() as db:
        return await db.scalar(
            text("SELECT coalesce(sum(delta_usd), 0) FROM tenant_ledger WHERE delta_usd < 0")
        )


async def test_pending_gemini_usage_is_debited_once_when_drained(sm):
    # Recorded with its infrastructure unknown, as every Gemini turn was until now.
    await pending_usage(sm)
    assert await _pending(sm) == 1 and await _debited(sm) == 0
    applied, usd = await drain_pending_usage(sm, markup=Decimal(1))
    assert applied == 1 and usd > 0
    assert await _pending(sm) == 0 and await _debited(sm) == -usd
    # Idempotent: a second pass applies nothing and debits nothing.
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await _debited(sm) == -usd


async def _spend(sm, binding):
    tenant_id, since = UUID(binding.thread.channel.tenant_id), datetime.now(UTC) - timedelta(1)
    async with sm() as db:
        rows = await usage_events.list_for_tenant(db, tenant_id=tenant_id)
        total = await usage_events.cost_for_tenant_since(db, tenant_id=tenant_id, since=since)
        by_agent = await usage_events.costs_by_agent_in_tenant_since(
            db, tenant_id=tenant_id, since=since
        )
    return rows, total, by_agent


async def _seat_agent(sm, binding, agent_id, *, owner=None):
    """A thread session for `binding`'s thread id, in `owner`'s tenant when given."""
    owner = owner or binding
    async with sm.begin() as db:
        await thread_sessions.create_thread_session(
            db,
            tenant_id=UUID(owner.thread.channel.tenant_id),
            platform=binding.thread.channel.platform,
            thread_id=binding.thread.thread_id,
            account_id=UUID(owner.legacy_account_id),
            ma_session_id="ma-" + binding.thread.thread_id,
            ma_agent_id=agent_id,
        )


async def test_drained_usage_shows_as_spent_on_the_agent_of_its_thread(sm):
    """The drain records no person; the workspace and per-agent spend still count the row,
    on the agent whose thread produced it, priced at the dated tariff its debit used."""
    binding = await pending_usage(sm)
    await _seat_agent(sm, binding, "ag_drained")
    _, usd = await drain_pending_usage(sm, markup=Decimal(1))
    rows, total, by_agent = await _spend(sm, binding)
    assert [r.platform_user_id for r in rows] == [None], "a drained row has no person"
    assert usd > 0 and total == pytest.approx(float(usd)), "spent equals the debit"
    assert by_agent == {"ag_drained": pytest.approx(float(usd))}, (
        "the agent card counts the drained row"
    )


async def test_drained_usage_stays_unattributed_when_no_agent_is_known(sm):
    """No other usage row and no thread session: the row counts in the total, not an agent."""
    binding = await pending_usage(sm)
    _, usd = await drain_pending_usage(sm, markup=Decimal(1))
    _, total, by_agent = await _spend(sm, binding)
    assert by_agent == {None: pytest.approx(float(usd))}, "unknown agent stays None"
    assert total == pytest.approx(float(usd)), "and still adds up to the total"


async def test_drained_usage_stays_unattributed_when_two_agents_shared_its_thread(sm):
    """Two agents ran in the thread: guessing one would misstate both cards."""
    binding = await pending_usage(sm)
    await _seat_agent(sm, binding, "ag_first")
    await _seat_agent(sm, binding, "ag_second")
    await drain_pending_usage(sm, markup=Decimal(1))
    _, _, by_agent = await _spend(sm, binding)
    assert set(by_agent) == {None}, "an ambiguous thread names no agent"


async def test_drained_usage_ignores_an_agent_of_another_tenant(sm):
    """A thread with the same id in another tenant never lends its agent."""
    binding = await pending_usage(sm)
    other = await seed_binding(sm)
    await _seat_agent(sm, binding, "ag_other_tenant", owner=other)
    await drain_pending_usage(sm, markup=Decimal(1))
    _, _, by_agent = await _spend(sm, binding)
    assert set(by_agent) == {None}, "attribution is tenant-scoped"


async def test_the_drain_leaves_a_profile_with_unknown_infrastructure_pending(sm):
    """An OpenAI persistent workspace runs a hosted container: its cost stays unknown."""
    await pending_usage(sm, provider="openai")
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await _pending(sm) == 1 and await _debited(sm) == 0


async def test_rows_the_drain_cannot_price_never_block_the_rest(sm):
    """Partial counts can't be priced; with a one-row batch, the priceable row behind
    them is still reached within a few passes (the pass resumes where the last stopped)."""
    for _ordinal in range(3):  # partial: no output count, so no price
        await pending_usage(sm, outputs=None)
    await pending_usage(sm)
    assert await _pending(sm) == 4
    applied = 0
    for _ in range(6):
        applied += (await drain_pending_usage(sm, markup=Decimal(1), limit=1))[0]
    assert applied == 1 and await _pending(sm) == 3


async def test_a_binding_that_ever_had_another_profile_stays_pending(sm):
    """A binding id survives a rebind: an earlier persistent-workspace generation's row
    must not be settled as if it were inline reuse at $0 infrastructure."""
    from daimon.core._models import ProviderBinding
    from sqlalchemy import insert, select

    binding = await pending_usage(sm)
    async with sm() as db, db.begin():
        current = (
            await db.execute(
                select(ProviderBinding).where(ProviderBinding.binding_id == binding.id)
            )
        ).scalar_one()
        await db.execute(
            insert(ProviderBinding).values(
                binding_id=current.binding_id,
                generation=current.generation - 1,
                tenant_id=current.tenant_id,
                provider="openai",
                profile="openai.persistent_workspace",
                binding=current.binding,
            )
        )
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await _pending(sm) == 1 and await _debited(sm) == 0


async def test_concurrent_drains_apply_a_row_once(sm):
    import asyncio

    await pending_usage(sm)
    results = await asyncio.gather(*(drain_pending_usage(sm, markup=Decimal(1)) for _ in range(4)))
    assert sum(applied for applied, _ in results) == 1
    usd = sum(debited for _, debited in results)
    assert usd > 0 and await _debited(sm) == -usd and await _pending(sm) == 0
