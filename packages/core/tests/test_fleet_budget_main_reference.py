"""The batched fleet budget producer against main's own code from before batching.

`test_fleet_budget_batching` compares the batched producer with a patched copy (no
prefetch, the previous `save_budget`). This compares it with main as of REFERENCE,
loaded from git: `_financial_room` read before any write, the reservation re-reading the
root finances, every per-worker read, and `save_budget` re-reading every run. The
reference modules are wired only to each other, so no batched code runs in them.
"""

import subprocess
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from daimon.core import channel_budget
from daimon.core.stores import channel_budgets, fleet

from .test_fleet_budget import POLICY, add, reserve, setup, tariffs, usage
from .test_fleet_budget import sm as sm
from .test_fleet_budget_batching import (
    _claim,
    _resolved,
    _rolled_back,
    _usages,
    rich_graph,
)

#: Main just before the batching (#1465).
REFERENCE = "56fc2368c"
REPO = Path(__file__).resolve().parents[3]
#: In dependency order: each module's imports of an earlier one are rebound to its copy.
MODULES = (
    "daimon.core.stores",
    "daimon.core.stores.channel_budgets",
    "daimon.core.stores.tenant_ledger",
    "daimon.core.stores.mux_state",
    "daimon.core.stores.fleet",
    "daimon.core.stores.turn_usage",
    "daimon.core.usage_delegation",
    "daimon.core.channel_budget",
)


def _source(name: str) -> str:
    path = "packages/core/" + name.replace(".", "/")
    path += "/__init__.py" if name == "daimon.core.stores" else ".py"
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{REFERENCE}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(scope="module")
def main() -> types.ModuleType:
    """main's `channel_budget` as of REFERENCE, over main's stores of the same commit."""
    loaded: dict[str, types.ModuleType] = {}
    for name in MODULES:
        alias = f"_reference_{REFERENCE}.{name}"
        module = types.ModuleType(alias)
        module.__package__ = name if name == "daimon.core.stores" else name.rpartition(".")[0]
        sys.modules[alias] = module
        exec(compile(_source(name), f"{REFERENCE}:{name}", "exec"), module.__dict__)
        for key, value in list(vars(module).items()):
            if isinstance(value, types.ModuleType) and value.__name__ in loaded:
                setattr(module, key, loaded[value.__name__])
            elif getattr(value, "__module__", None) in loaded:
                origin = loaded[value.__module__]
                setattr(module, key, getattr(origin, getattr(value, "__name__", key), value))
        loaded[name] = module
    reference = loaded["daimon.core.channel_budget"]
    # Nothing batched is reachable from the reference.
    assert not hasattr(reference, "_prefetch_fleet_money")
    assert not hasattr(reference.fleet_store, "BudgetPrefetch")
    assert reference.read_accounting_revisions.__module__.startswith("_reference_")
    assert reference.fleet_store.save_budget.__module__.startswith("_reference_")
    return reference


async def _reviewer(sm, parent, run, *, ordinal: int, of: Any, running: bool):
    """A reviewer of `of`: reserved and pending, or claimed, bound and running with usage."""
    async with sm() as db, db.begin():
        reserved = await channel_budget.admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=_resolved(parent),
            ordinal=ordinal,
            role="reviewer",
            prices=tariffs(parent),
            markup=Decimal(1),
            reviewer_of=of.id,
        )
    assert reserved.admitted, reserved.reason
    worker = reserved.worker
    if not running:
        return worker
    claimed = await _claim(sm, run, worker)
    await usage(
        sm, parent, run, worker, inputs=4000, outputs=400, revision=1, infrastructure=Decimal(0)
    )
    return await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id=f"worker-{worker.id}",
    )


async def reviewer_graph(sm):
    """The rich graph plus reviewers: a pending one on the caller's run, a running bound
    one with settled usage on run 3."""
    graph = await rich_graph(sm)
    (run1, _, run3), (p1, _, p3) = graph.runs, graph.parents
    _, workers1 = await fleet.run_status(sm, tenant_id=run1.tenant_id, run_id=run1.id)
    _, workers3 = await fleet.run_status(sm, tenant_id=run3.tenant_id, run_id=run3.id)
    first1 = next(w for w in workers1 if w.ordinal == 0)
    first3 = next(w for w in workers3 if w.ordinal == 0)
    await _reviewer(sm, p1, run1, ordinal=5, of=first1, running=False)
    await _reviewer(sm, p3, run3, ordinal=3, of=first3, running=True)
    return graph


CALLS = (
    "admit_new",
    "admit_replay",
    "admit_reviewer",
    "reserve_producer",
    "require_bound",
    "require_starting",
    "passive",
    "snapshot",
)


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("shape", ["rich", "reviewers"])
async def test_batched_producer_equals_main(sm, main, call, shape):
    graph = await (rich_graph(sm) if shape == "rich" else reviewer_graph(sm))
    parent, run = graph.parent, graph.runs[0]
    tenant_id = run.tenant_id
    prices = tariffs(parent)
    _, workers = await fleet.run_status(sm, tenant_id=tenant_id, run_id=run.id)
    starting = next(w for w in workers if w.status == "starting")
    pending = next(w for w in workers if w.status == "pending" and w.reviewer_of is None)
    reviewed = next(w for w in workers if w.ordinal == 0)

    def invoke(module: types.ModuleType):
        async def call_it(db):
            if call in ("admit_new", "admit_replay", "admit_reviewer"):
                target = pending if call == "admit_replay" else None
                return await module.admit_fleet_worker(
                    db,
                    parent=parent,
                    policy=POLICY,
                    resolved=_resolved(parent),
                    ordinal=target.ordinal if target else max(w.ordinal for w in workers) + 1,
                    role="reviewer" if call == "admit_reviewer" else "worker",
                    prices=prices,
                    markup=Decimal(1),
                    reviewer_of=reviewed.id if call == "admit_reviewer" else None,
                )
            if call == "reserve_producer":
                await module.fleet_store.lock_budget(
                    db,
                    tenant_id=tenant_id,
                    actor_id=parent.fleet.account_id,
                    run_id=run.id,
                    persisted=True,
                )
                return await module._produce_fleet_budget(
                    db,
                    parent=parent,
                    worker_id=pending.id,
                    prices=prices,
                    markup=Decimal(1),
                    reserve=True,
                    financial=True,
                )
            if call in ("require_bound", "require_starting"):
                bound = call == "require_bound"
                return await module.require_fleet_worker_admission(
                    db,
                    parent=parent,
                    policy=POLICY,
                    worker_id=graph.bound.id if bound else starting.id,
                    prices=prices,
                    markup=Decimal(1),
                    binding=graph.binding if bound else None,
                )
            if call == "passive":
                return await module.produce_fleet_budget(
                    db, parent=parent, prices=prices, markup=Decimal(1)
                )
            raise AssertionError(call)

        return call_it

    if call == "snapshot":
        batched, reference = [
            _usages(
                await module.read_fleet_budget_snapshot(
                    sm, parent=parent, prices=prices, markup=Decimal(1)
                )
            )
            for module in (channel_budget, main)
        ]
    else:
        batched = await _rolled_back(sm, invoke(channel_budget), tenant_id)
        reference = await _rolled_back(sm, invoke(main), tenant_id)
    assert batched == reference


# --- Refusals: the same reason, at the same point, with nothing else written ---------


async def _tenant_short(sm):
    # $1.50 of funds cannot cover a $3 run bound.
    return await setup(sm, funds="1.5", cap="3")


async def _channel_short(sm):
    parent, run = await setup(sm, funds="10", cap="3")
    async with sm() as db, db.begin():
        await channel_budgets.set_channel_budget(
            db,
            tenant_id=run.tenant_id,
            platform=run.platform,
            channel_id=run.channel_id,
            limit_usd=Decimal(".5"),
            window="total",
            starts_at=None,
            ends_at=None,
            set_by_account_id=parent.fleet.account_id,
        )
    return parent, run


async def _run_short(sm):
    # Two $1 workers already hold $2 of a $2.50 run bound; a third does not fit.
    parent, run = await setup(sm, funds="10", cap="2.5")
    for ordinal in range(2):
        assert (await reserve(sm, parent, await add(sm, parent, ordinal=ordinal))).admitted
    return parent, run


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (_tenant_short, "tenant_budget_exceeded"),
        (_channel_short, "channel_budget_exceeded"),
        (_run_short, "run_budget_exceeded"),
    ],
)
async def test_a_refused_admission_refuses_as_main_does(sm, main, build, reason):
    parent, run = await build(sm)
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)

    def admit(module: types.ModuleType):
        async def call_it(db):
            return await module.admit_fleet_worker(
                db,
                parent=parent,
                policy=POLICY,
                resolved=_resolved(parent),
                ordinal=len(workers),
                role="worker",
                prices=tariffs(parent),
                markup=Decimal(1),
            )

        return call_it

    batched = await _rolled_back(sm, admit(channel_budget), run.tenant_id)
    reference = await _rolled_back(sm, admit(main), run.tenant_id)
    assert batched == reference
    assert f"'reason': '{reason}'" in batched[0] and "'admitted': False" in batched[0]
