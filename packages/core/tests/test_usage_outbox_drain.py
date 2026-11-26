"""Pending ordinary provider usage settles exactly once when its cost is known."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core import usage_outbox_drain
from daimon.core.stores import mux_state, thread_sessions, usage_events
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
    """A drained row keeps the person and budget channel it was recorded with, so their
    allowance and budget count it; the workspace and per-agent spend count it too, on
    the agent whose thread produced it, priced at the dated tariff its debit used."""
    binding = await pending_usage(sm, channel_id="C-budget")
    await _seat_agent(sm, binding, "ag_drained")
    _, usd = await drain_pending_usage(sm, markup=Decimal(1))
    rows, total, by_agent = await _spend(sm, binding)
    assert [r.platform_user_id for r in rows] == ["tester"], "the person who asked"
    async with sm() as db:
        channels = (await db.execute(text("SELECT channel_id FROM tenant_ledger"))).scalars()
        assert set(channels) == {"C-budget"}, "debited to the recorded budget channel"
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


async def test_pending_openai_workspace_usage_drains_at_zero_infrastructure(sm):
    """No meter reports a persistent workspace's container fee: its tokens settle at $0
    infrastructure instead of staying pending forever."""
    await pending_usage(sm, provider="openai")
    applied, usd = await drain_pending_usage(sm, markup=Decimal(1))
    assert applied == 1 and usd > 0, "the pending OpenAI row is debited"
    assert await _pending(sm) == 0 and await _debited(sm) == -usd, "exactly once"


_MEASURED = {"completeness": "measured", "input_tokens": 1000, "output_tokens": 100}
_MEASURED |= {"input_cached_tokens": 0, "input_cache_write_tokens": 0}


async def _reread(
    sm,
    monkeypatch,
    *,
    seen_ago: timedelta,
    budget_s: float = 10.0,
    stall: bool = False,
    final: bool = True,
    legacy: bool = False,
) -> tuple[int, list[str]]:
    """One durable re-read pass over an unknown OpenAI turn, answered as measured."""
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.openai_late_usage import DurableRereads
    from mux.drivers.openai import OpenAIUsage

    read: list[str] = []

    async def reconcile_turn(self, scope, session, turn_id):
        read.append(turn_id)
        if stall:
            await asyncio.Event().wait()
        return (pending.model_copy(update=measured),)

    monkeypatch.setattr(openai_deployment, "usage_transport", lambda *_: object())
    monkeypatch.setattr(OpenAIUsage, "reconcile_turn", reconcile_turn)
    await pending_usage(
        sm, provider="openai", unknown=True, observed_at=datetime.now(UTC) - seen_ago
    )
    if legacy:  # as written before observations carried these (0216)
        async with sm.begin() as db:
            await db.execute(
                text("UPDATE usage_observation SET provider = NULL, observed_at = NULL")
            )
    async with sm() as db:
        row = (await mux_state.pending_outbox(db))[0]
    pending = row.observation
    measured = _MEASURED | {"revision": 2, "final": final}
    reads = await DurableRereads(budget_s=budget_s).run(
        sm, markup=Decimal(1), now=datetime.now(UTC)
    )
    return reads, read


@pytest.mark.parametrize("legacy", [False, True], ids=["seen", "written_before_0216"])
async def test_unknown_openai_usage_is_reread_from_the_scheduler_and_debited(
    sm, monkeypatch, legacy
):
    reads, read = await _reread(sm, monkeypatch, seen_ago=timedelta(hours=3), legacy=legacy)
    assert reads == 1 and read == ["turn"]
    debited = await _debited(sm)
    assert debited < 0
    # The scheduler's drain, next in the same step, closes the unknown revision.
    await drain_pending_usage(sm, markup=Decimal(1))
    assert await _pending(sm) == 0 and await _debited(sm) == debited


@pytest.mark.parametrize("priced", [True, False], ids=["settled", "unpriced"])
async def test_a_turn_whose_usage_is_final_is_not_reread_by_a_later_pass(sm, monkeypatch, priced):
    """A settled final revision closes the older unknown one; an unpriced final one stays
    pending, and either way a later pass reads the turn no more."""
    from daimon.core import pricing
    from daimon.core.turn.openai_late_usage import DurableRereads

    if not priced:
        monkeypatch.setattr(pricing, "provider_prices", lambda _profile: {})
    _, read = await _reread(sm, monkeypatch, seen_ago=timedelta(hours=3))
    assert read == ["turn"], "the unknown usage is read once"
    assert await _pending(sm) == (0 if priced else 2), "only an unsettled revision stays"
    later = await DurableRereads().run(sm, markup=Decimal(1), now=datetime.now(UTC))
    assert later == 0 and read == ["turn"], "a final turn is never read again"


@pytest.mark.parametrize("billed_by", ["turn", "reread"])
async def test_a_billed_fallback_is_reread_after_a_restart_until_final(sm, monkeypatch, billed_by):
    """A fallback billed as the turn ended, or by a re-read whose unknown revision the drain
    then closed, leaves nothing pending; a restarted scheduler still reads the turn until
    its usage is final, and the final reading charges nothing twice."""
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.openai_late_usage import DurableRereads
    from mux.drivers.openai import OpenAIUsage

    seen_ago = timedelta(hours=3)
    if billed_by == "turn":
        await pending_usage(sm, provider="openai", observed_at=datetime.now(UTC) - seen_ago)
    else:
        await _reread(sm, monkeypatch, seen_ago=seen_ago, final=False)
    async with sm() as db:
        row = (await mux_state.pending_outbox(db))[0]
    await drain_pending_usage(sm, markup=Decimal(1))
    debited = await _debited(sm)
    assert debited < 0 and await _pending(sm) == 0, "the fallback is billed, nothing pending"
    async with sm() as db:
        newest = await mux_state.get_newest_usage_revision(db, row.binding_id, row.observation_id)
    assert newest is not None
    # Twice the fallback's tokens, so a charge of the whole reading would show.
    doubled = {"input_tokens": 2000, "output_tokens": 200, "revision": newest + 1, "final": True}
    final = row.observation.model_copy(update=_MEASURED | doubled)
    read: list[str] = []

    async def reconcile_turn(self, scope, session, turn_id):
        read.append(turn_id)
        return (final,)

    monkeypatch.setattr(openai_deployment, "usage_transport", lambda *_: object())
    monkeypatch.setattr(OpenAIUsage, "reconcile_turn", reconcile_turn)
    now = datetime.now(UTC)
    assert await DurableRereads().run(sm, markup=Decimal(1), now=now) == 1, "read after restart"
    assert await _debited(sm) == 2 * debited, "the final reading charges only what's new"
    assert await DurableRereads().run(sm, markup=Decimal(1), now=now) == 0, "final ends it"
    assert read == ["turn"], "read once more after the restart, then never"


async def test_a_billed_fallback_whose_thread_moved_is_read_once(sm, monkeypatch):
    """Its row stays listed while not final; a pass's reads must not go on it again."""
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.openai_late_usage import DurableRereads

    await pending_usage(sm, provider="openai", observed_at=datetime.now(UTC) - timedelta(hours=3))
    await drain_pending_usage(sm, markup=Decimal(1))
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE provider_binding SET binding = "
                "jsonb_set(binding, '{native_refs,session}', '\"moved\"')"
            )
        )
    monkeypatch.setattr(openai_deployment, "usage_transport", lambda *_: object())
    rereads, now = DurableRereads(), datetime.now(UTC)
    assert await rereads.run(sm, markup=Decimal(1), now=now) == 1, "the move is found"
    assert await rereads.run(sm, markup=Decimal(1), now=now) == 0, "and read no more"


async def test_a_stalled_reread_ends_the_pass_at_its_time_budget(sm, monkeypatch):
    reads, read = await _reread(
        sm, monkeypatch, seen_ago=timedelta(hours=3), budget_s=0.05, stall=True
    )
    assert reads == 1 and read == ["turn"]
    assert await _pending(sm) == 1 and await _debited(sm) == 0


async def test_a_reread_cut_off_by_the_pass_budget_backs_off_and_the_next_row_is_read(
    sm, monkeypatch
):
    """The cut-off row waits out its backoff instead of taking the next pass's whole budget."""
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.openai_late_usage import DurableRereads
    from mux.drivers.openai import OpenAIUsage

    first: list[str] = []

    async def reconcile_turn(self, scope, session, turn_id):
        first.append(session.id)
        if session.id == first[0]:  # the first row read is the slow one
            await asyncio.Event().wait()
        return (pending.model_copy(update=measured | {"session": session}),)

    monkeypatch.setattr(openai_deployment, "usage_transport", lambda *_: object())
    monkeypatch.setattr(OpenAIUsage, "reconcile_turn", reconcile_turn)
    seen = datetime.now(UTC) - timedelta(hours=3)
    for _ in range(2):
        await pending_usage(sm, provider="openai", unknown=True, observed_at=seen)
    async with sm() as db:
        pending = (await mux_state.pending_outbox(db))[0].observation
    measured = _MEASURED | {"revision": 2, "final": True}
    rereads, now = DurableRereads(budget_s=1.0), datetime.now(UTC)  # room for a real read
    assert await rereads.run(sm, markup=Decimal(1), now=now) == 1, "the slow read is cut off"
    assert await _debited(sm) == 0, "nothing was read through"
    later = now + timedelta(seconds=1)
    assert await rereads.run(sm, markup=Decimal(1), now=later) == 1, "only the other row is due"
    assert await _debited(sm) < 0, "the row behind the slow one was read"


async def test_unknown_openai_usage_is_left_to_the_process_that_ran_the_turn(sm, monkeypatch):
    """Within its hold-off the scheduler leaves the turn to the process that ran it."""
    reads, read = await _reread(sm, monkeypatch, seen_ago=timedelta(minutes=5))
    assert (reads, read) == (0, []), "the scheduler read a turn the bot still re-reads"
    assert await _pending(sm) == 1 and await _debited(sm) == 0


async def test_unknown_openai_usage_just_past_the_hold_off_is_read(sm, monkeypatch):
    reads, read = await _reread(sm, monkeypatch, seen_ago=timedelta(minutes=11))
    assert (reads, read) == (1, ["turn"]), "the scheduler takes a turn after its hold-off"
    assert await _debited(sm) < 0, "the read usage was debited"


async def test_unknown_openai_usage_is_given_up_a_day_after_it_was_seen(sm, monkeypatch):
    reads, read = await _reread(sm, monkeypatch, seen_ago=timedelta(days=2))
    assert reads == 0 and read == []
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
    """A binding id survives a rebind: an earlier self-hosted generation's row must not
    be settled as if it were inline reuse at $0 infrastructure."""
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
                provider="selfhosted",
                profile="selfhosted.loop",
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
