"""J3/J4/J10: usage that sat pending while its infrastructure cost was unknown is
settled once that cost is known to be $0, exactly once.

SMOKE fc0fa16a: 156 of 156 staging outbox rows (Gemini and OpenAI, since 10-15) were
never applied, so no Gemini/OpenAI turn was ever debited and every fleet run's spend
read as 0.
"""

from decimal import Decimal

from daimon.core.usage_outbox_drain import drain_pending_usage
from sqlalchemy import text

from .test_fleet_budget import root, setup
from .test_fleet_budget import sm as sm
from .test_fleet_budget_batching import _running


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
    parent, run = await setup(sm, funds="10", cap="2")
    assert parent.backend_grants[0].profile == "gemini.inline_reuse"
    # Recorded with its infrastructure unknown, as every Gemini turn was until now.
    await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    assert await _pending(sm) == 1 and await _debited(sm) == 0
    applied, usd = await drain_pending_usage(sm, markup=Decimal(1))
    assert applied == 1 and usd > 0
    assert await _pending(sm) == 0 and await _debited(sm) == -usd
    # Idempotent: a second pass applies nothing and debits nothing.
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await _debited(sm) == -usd


async def test_the_drain_leaves_a_profile_with_unknown_infrastructure_pending(sm):
    """An OpenAI persistent workspace runs a hosted container: its cost stays unknown."""
    parent, run = await setup(sm, parent=root("openai"), funds="10", cap="2")
    assert parent.backend_grants[0].profile == "openai.persistent_workspace"
    await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await _pending(sm) == 1 and await _debited(sm) == 0


async def test_rows_the_drain_cannot_price_never_block_the_rest(sm):
    """Partial counts can't be priced; with a one-row batch, the priceable row behind
    them is still reached within a few passes (the pass resumes where the last stopped)."""
    parent, run = await setup(sm, funds="10", cap="8", max_workers=12)
    for ordinal in range(3):  # partial: no output count, so no price
        await _running(sm, parent, run, ordinal, revisions=((1, 1000, None),), settled=False)
    await _running(sm, parent, run, 3, revisions=((1, 1000, 100),), settled=False)
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

    parent, run = await setup(sm, funds="10", cap="2")
    worker = await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    async with sm() as db, db.begin():
        current = (
            await db.execute(
                select(ProviderBinding).where(ProviderBinding.binding_id == f"worker-{worker.id}")
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

    parent, run = await setup(sm, funds="10", cap="2")
    await _running(sm, parent, run, 0, revisions=((1, 1000, 100),), settled=False)
    results = await asyncio.gather(*(drain_pending_usage(sm, markup=Decimal(1)) for _ in range(4)))
    assert sum(applied for applied, _ in results) == 1
    usd = sum(debited for _, debited in results)
    assert usd > 0 and await _debited(sm) == -usd and await _pending(sm) == 0
