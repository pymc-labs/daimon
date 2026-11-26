"""A late debit reaches a waiting footer once its edit lands, and only while it waits."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core.pricing import provider_prices
from daimon.core.provider_turn_cost import LateTurnCosts
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.profiles.openai import PERSISTENT_WORKSPACE


def _reading(revision: int, *, final: bool = False) -> UsageObservation:
    measured = revision > 1
    return UsageObservation(
        id="usage",
        revision=revision,
        session=ResourceRef(id="s", kind="session", provider="openai", account_scope_id="a"),
        model={"provider": "openai", "id": "gpt-6-luna"},
        grain="turn",
        basis="cumulative",
        input_tokens=100_000 if measured else None,
        input_cached_tokens=0 if measured else None,
        output_tokens=10_000 * revision if measured else None,
        completeness="measured" if measured else "unknown",
        observed_at=datetime.now(UTC),
        final=final,
    )


def _settle(costs: LateTurnCosts, reading: UsageObservation) -> None:
    price = provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", "gpt-6-luna")]
    costs.settle(reading, price=price, infrastructure_usd=Decimal(0), markup=Decimal(1))


async def test_a_refused_edit_is_retried_and_a_newer_revision_wins() -> None:
    shown, answers = list[float](), [False, True, True]

    async def show(debit: float) -> bool:
        shown.append(debit)
        return answers.pop(0)

    costs = LateTurnCosts(retry_s=(0.0,))
    costs.turn_debit(
        ProviderUsageTotals().add_observation(_reading(1)), markup=Decimal(1), show=show
    )
    _settle(costs, _reading(2))
    await costs.wait()
    _settle(costs, _reading(2))  # already shown: no edit
    _settle(costs, _reading(3, final=True))
    await costs.wait()
    assert shown == [0.02, 0.02, 0.025], shown


async def test_a_footer_past_its_rereads_takes_no_debit() -> None:
    now, shown = [0.0], list[float]()

    async def show(debit: float) -> bool:
        shown.append(debit)
        return True

    costs = LateTurnCosts(max_age_s=10.0, clock=lambda: now[0])
    costs.turn_debit(
        ProviderUsageTotals().add_observation(_reading(1)), markup=Decimal(1), show=show
    )
    now[0] = 10.0
    _settle(costs, _reading(2, final=True))
    await costs.wait()
    assert shown == [], shown


async def _yield() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_overlapping_settles_make_one_edit_at_a_time_and_the_newest_debit_wins() -> None:
    """A repeat or newer debit while an edit runs starts no second edit; the newest lands last."""
    gate, shown, running, peak = asyncio.Event(), list[float](), [0], [0]

    async def show(debit: float) -> bool:
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        shown.append(debit)
        await gate.wait()
        running[0] -= 1
        return True

    costs = LateTurnCosts()
    costs.turn_debit(
        ProviderUsageTotals().add_observation(_reading(1)), markup=Decimal(1), show=show
    )
    _settle(costs, _reading(2))
    while not shown:
        await asyncio.sleep(0)
    _settle(costs, _reading(2))  # the same debit again, while its edit is in flight
    await _yield()
    _settle(costs, _reading(3))
    _settle(costs, _reading(4, final=True))
    await _yield()
    gate.set()
    await costs.wait()
    assert peak == [1], f"edits of one footer overlapped: {peak[0]} at once"
    assert shown == [0.02, 0.03], shown


async def test_a_final_debit_settled_during_the_last_retry_of_an_older_one_is_shown() -> None:
    """The edit in flight ran out of tries for the older debit; the newest gets its own."""
    calls, gate = list[float](), asyncio.Event()

    async def show(debit: float) -> bool:
        calls.append(debit)
        if len(calls) == 4:  # the last try of the first debit
            await gate.wait()
        return len(calls) > 4

    costs = LateTurnCosts(retry_s=(0.0, 0.0, 0.0))
    costs.turn_debit(
        ProviderUsageTotals().add_observation(_reading(1)), markup=Decimal(1), show=show
    )
    _settle(costs, _reading(2))
    while len(calls) < 4:
        await asyncio.sleep(0)
    _settle(costs, _reading(3, final=True))
    gate.set()
    await costs.wait()
    assert calls == [0.02] * 4 + [0.025], calls
