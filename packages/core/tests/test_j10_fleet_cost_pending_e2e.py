"""J10 acceptance: the fleet's cost is never shown as final while a worker's usage is
still being counted, and the settled figure is exactly what the workers were debited.

Staging rehearsal 6328714f (build 36739621): the card said "Cost $0.01" at 06:46 and
the root replied "This cost $0.01."; Astra's turn had ended with `usage: null`, so its
usage was pending. The re-read settled it at 06:51 ($0.169873); the run's real cost was
$0.18, and nothing told anyone.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core.agent_model_names import model_name
from daimon.core.channel_budget import FleetCost, read_ended_run_cost, read_fleet_run_cost
from daimon.core.fleet_orchestration import fleet_cost_line, fleet_cost_sentence, fleet_cost_words
from daimon.core.usage_outbox_drain import drain_pending_usage
from daimon.core.usage_reread import reread_unmeasured_usage

from .test_fleet_budget import setup
from .test_fleet_budget import sm as sm
from .test_j10_fleet_settles_every_provider_e2e import (
    _debits,
    _gemini_interaction,
    _openai_turn,
    _parent,
    _prices,
    _record,
    _send,
    _worker,
)
from .test_j10_settle_evidence_e2e import _end


async def _rehearsal(sm):
    """Staging's shape: Gemini metered and applied; the OpenAI worker's only turn read
    as null (Astra in the rehearsal; the journey helpers run Luna)."""
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="2", max_workers=4)
    prices = _prices(parent)
    argon_model, astra_model = parent.models[0], parent.models[1]
    argon, argon_binding, argon_native = await _worker(sm, parent, run, 0, argon_model, prices)
    astra, astra_binding, astra_native = await _worker(sm, parent, run, 1, astra_model, prices)
    for revision in (1, 2):
        await _record(
            sm,
            run,
            argon_binding,
            _gemini_interaction(argon_native, argon_model, "v1_argon", revision=revision),
            prices,
        )
    await _send(sm, run, argon, status="accepted", turn_id="v1_argon")
    assert not await _record(
        sm, run, astra_binding, _openai_turn(astra_native, "turn_astra", metered=False), prices
    )
    await _send(sm, run, astra, status="processed")
    await _end(sm, run, argon, astra)
    return parent, run, prices, astra_native


async def test_a_cost_with_a_worker_still_being_counted_is_never_final(sm):
    parent, run, prices, _ = await _rehearsal(sm)
    cost = await read_fleet_run_cost(sm, parent=parent, prices=prices, markup=Decimal(1))
    assert cost is not None
    # Argon's debit only, and Astra named as still being counted.
    openai = model_name(parent.models[1])  # the helpers' OpenAI worker model
    assert cost == FleetCost(await _debits(sm, run.tenant_id), (openai,))
    assert fleet_cost_line(cost) == (
        f"Cost so far less than $0.01 · {openai}'s cost is still being counted"
    )
    assert fleet_cost_sentence(cost) == (
        f"This cost less than $0.01 so far; {openai}'s cost is still being counted."
    )
    # Gemini's rev1 "unknown" is superseded by its applied rev2 even before the drain
    # closes it: Argon is not pending, so it isn't named.
    assert "Gemini 4 Argon" not in fleet_cost_line(cost)


async def test_once_the_reread_settles_it_the_cost_is_exactly_the_workers_debits(sm):
    parent, run, prices, astra_native = await _rehearsal(sm)

    async def metered(native, revision):
        return [_openai_turn(native, "turn_astra", revision=revision("openai:turn:turn_astra"))]

    assert (
        await reread_unmeasured_usage(
            sm,
            markup=Decimal(1),
            list_usage=metered,
            now=datetime.now(UTC) + timedelta(minutes=6),
        )
        == 1
    )
    await drain_pending_usage(sm, markup=Decimal(1))
    # Read as the run-end poller does: by tenant and run, with no frozen root.
    settled = await read_ended_run_cost(
        sm, tenant_id=run.tenant_id, run_id=run.id, prices=prices, markup=Decimal(1)
    )
    assert settled is not None and settled.pending == ()
    assert settled.usd == await _debits(sm, run.tenant_id) > 0
    assert fleet_cost_line(settled) == f"Cost {fleet_cost_words(settled.usd)}"
    assert fleet_cost_sentence(settled).startswith("This cost ") and "so far" not in (
        fleet_cost_sentence(settled)
    )


async def test_a_worker_that_never_sent_is_not_still_being_counted(sm):
    from .test_j3_fleet_terminal_settlement_e2e import _claimed_unbound

    parent, run = await setup(sm, funds="10", cap="2")
    await _claimed_unbound(sm, parent, run, 0)
    cost = await read_fleet_run_cost(
        sm, parent=parent, prices=_prices_for(parent), markup=Decimal(1)
    )
    assert cost == FleetCost(Decimal(0))
    assert fleet_cost_sentence(cost) == "This cost $0.00."


def _prices_for(parent):
    from .test_fleet_budget import tariffs

    return tariffs(parent)


def test_several_models_still_being_counted_are_all_named():
    cost = FleetCost(Decimal("0.42"), ("GPT-6 Astra", "Gemini 4 Argon"))
    assert fleet_cost_line(cost) == (
        "Cost so far $0.42 · GPT-6 Astra's and Gemini 4 Argon's costs are still being counted"
    )
    assert fleet_cost_line(None) == "Cost not available yet"
    assert fleet_cost_line(FleetCost(Decimal("0.425"))) == "Cost $0.43"
