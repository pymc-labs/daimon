"""J10 + fleet wizard PR 1, offline: with `fleet.require_plan` on, a member's fleet runs
only under a plan they accepted, and inside it (lanes/V2/FLEET-WIZARD-DESIGN.md).

The real fleet tools over the J10 root (a member, a fresh channel, the code defaults):
fleet_plan builds the defaults from the agent's fleet limits; an unplanned spawn is
refused (FW-c); an accepted plan, edited to a lower budget, is what each worker gets
(FW-b); a spawn past the plan, or of a model it doesn't name, is refused; accepting is
one-shot. Cheap models only (the root's own Haiku).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.adapters.mcp.tools.fleet import HostFleetToolService, PlanWorker
from daimon.core.config import FleetSettings
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.fleet_plan import PlanCaps, PlannedWorker, PlanRefused, build_plan, edit_plan
from daimon.core.stores.domain import Role
from daimon.core.stores.fleet import WorkerPool
from daimon.core.stores.fleet_plans import accept_plan, pending_plan
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError

from packages.adapters.mcp.tests.tools.test_fleet import AUTH, runtime
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import j10_root as j10_root
from packages.core.tests.turn.test_frozen_root import run_root_input


async def _fleet(j10_root, *, require_plan: bool):
    """The member's frozen root, and the fleet tools over it."""
    deps, prepared, _, _, seed, scope, _, _ = j10_root
    origin_cm = turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        keep_fleet_root=True,
    )
    origin = await origin_cm.__aenter__()
    await run_root_input((*j10_root[:4], origin, *j10_root[5:]))
    root, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    ports = HostFleetPorts(
        deps.sessionmaker,
        markup=Decimal(1),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=4, max_concurrent_workers_per_tenant=4),
    )
    base = runtime()
    settings = base.settings.model_copy(update={"fleet": FleetSettings(require_plan=require_plan)})
    bridge = HostFleetToolService(
        FleetOrchestrator(ports),
        runtime=replace(
            base, session_factory=deps.sessionmaker, client=deps.anthropic, settings=settings
        ),
        deps=deps,
    )
    caller = replace(
        AUTH,
        tenant_id=origin.tenant_id,
        account_id=origin.account_id,
        role=Role.USER,
        chat_agent_id=root.agent_id,
    )
    return origin_cm, origin, root, ports, bridge, caller


async def _spawn(bridge, caller, origin, key: str, **kwargs):
    return dict(
        await bridge.spawn(
            caller,
            run_id=kwargs.pop("run_id", None),
            origin_context_id=origin.id,
            operation_key=key,
            role=kwargs.pop("role", "worker"),
            backend=None,
            agent=None,
            clone=None,
            clone_revision=None,
            overrides=None,
            session=None,
            budget_usd=kwargs.pop("budget_usd", None),
            deadline=None,
            max_turns=kwargs.pop("max_turns", None),
            result_contract=None,
            reviewer_of=kwargs.pop("reviewer_of", None),
            task="Say which model you run on, in one sentence.",
            **kwargs,
        )
    )


async def test_j10_with_plans_required_an_unplanned_fleet_is_refused(j10_root):
    """FW-c: the server, not the prompt, keeps a fleet from starting unplanned."""
    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        with pytest.raises(ToolError, match="needs a plan the person accepts first"):
            await _spawn(bridge, caller, origin, "W1")
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert not rows, "nothing was started"
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_an_accepted_plan_is_what_every_worker_gets(j10_root):
    """FW-b: the defaults come from the agent's fleet limits; the person lowers the
    budget; each worker spawned under the accepted plan gets that budget, and a spawn
    past the plan is refused."""
    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        own = root.spec.model.id
        made = dict(
            await bridge.plan(
                caller,
                workers=[PlanWorker(role="first", task="t1"), PlanWorker(role="second", task="t2")],
                origin_context_id=origin.id,
            )
        )
        assert made["status"] == "pending"
        plan_id = __import__("uuid").UUID(str(made["plan_id"]))
        default = Decimal(str(made["plan"]["budget_usd"]))
        caps_budget = Decimal(str(made["caps"]["budget_usd"]))
        assert default * 2 <= caps_budget, "an even share of the fleet's budget"
        assert [w["model"] for w in made["plan"]["workers"]] == [own, own]
        # A pending plan isn't accepted yet: a spawn under it is refused.
        with pytest.raises(ToolError, match="isn't accepted"):
            await _spawn(bridge, caller, origin, "W0", plan_id=plan_id)
        lowered = Decimal("0.05")
        async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
            pending = await pending_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                now=datetime.now(UTC),
            )
            assert pending is not None
            plan, caps = pending
            edited = edit_plan(plan, caps, budget_usd=lowered)
            assert await accept_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                plan=edited,
                now=datetime.now(UTC),
                ttl=timedelta(minutes=30),
            )
        async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
            assert not await accept_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                plan=edited,
                now=datetime.now(UTC),
                ttl=timedelta(minutes=30),
            ), "accepting is one-shot: a double tap accepts one plan"
        reply = await _spawn(bridge, caller, origin, "W1", plan_id=plan_id)
        assert reply["status"] == "pending"
        with pytest.raises(ToolError, match="goes past the accepted fleet plan"):
            await _spawn(bridge, caller, origin, "W2", plan_id=plan_id, budget_usd=Decimal("1"))
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert [row.budget_usd for row in rows] == [lowered], "the worker got the plan's budget"
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_with_plans_off_a_fleet_spawns_as_before(j10_root):
    """Off by default: today's fleet is unchanged (no plan asked for)."""
    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=False)
    try:
        reply = await _spawn(bridge, caller, origin, "W1")
        assert reply["status"] == "pending"
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


def test_a_plans_values_stay_inside_the_fleets_caps():
    """The person may change a plan, never past the agent's fleet limits; raising the
    limits is the fleet settings' job (owner or admin)."""
    caps = PlanCaps(
        max_workers=3,
        budget_usd=Decimal("0.30"),
        worker_seconds=600,
        max_turns=4,
        models=("claude-haiku-5-5", "gpt-6-luna", "gemini-3.8-flash"),
    )
    workers = tuple(
        PlannedWorker(role=f"w{i}", task="t", model=model) for i, model in enumerate(caps.models)
    )
    plan = build_plan(caps, workers)
    assert (plan.budget_usd, plan.worker_seconds, plan.max_turns) == (Decimal("0.10"), 600, 4)
    assert edit_plan(plan, caps, budget_usd=Decimal("0.05")).budget_usd == Decimal("0.05")
    with pytest.raises(PlanRefused, match="more than this fleet's"):
        edit_plan(plan, caps, budget_usd=Decimal("0.20"))
    with pytest.raises(PlanRefused, match="at most 10 min"):
        edit_plan(plan, caps, worker_seconds=3600)
    with pytest.raises(PlanRefused, match="at most 4 turns"):
        edit_plan(plan, caps, max_turns=9)
    with pytest.raises(PlanRefused, match="at most 3 workers"):
        build_plan(caps, (*workers, workers[0]))
    with pytest.raises(PlanRefused, match="isn't one of this fleet's models"):
        build_plan(caps, (PlannedWorker(role="x", task="t", model="claude-opus-5-5"),))


async def _accepted(bridge, caller, origin, workers: int, **edits):
    """A plan of `workers` task workers on the root's own model, accepted as edited."""
    import uuid

    made = dict(
        await bridge.plan(
            caller,
            workers=[PlanWorker(role=f"w{i}", task="t") for i in range(workers)],
            origin_context_id=origin.id,
        )
    )
    plan_id = uuid.UUID(str(made["plan_id"]))
    async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
        plan, caps = await pending_plan(
            session,
            plan_id=plan_id,
            tenant_id=caller.tenant_id,
            account_id=caller.account_id,
            now=datetime.now(UTC),
        )
        assert await accept_plan(
            session,
            plan_id=plan_id,
            tenant_id=caller.tenant_id,
            account_id=caller.account_id,
            plan=edit_plan(plan, caps, **edits),
            now=datetime.now(UTC),
            ttl=timedelta(minutes=30),
        )
    return plan_id


async def test_j10_an_accepted_plan_runs_its_workers_once_and_no_more(j10_root):
    """#1546 review 1: a plan isn't reusable forever. Past its workers a new spawn is
    refused; a retry of a started one is the same spawn."""
    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        plan_id = await _accepted(bridge, caller, origin, 1)
        assert (await _spawn(bridge, caller, origin, "W1", plan_id=plan_id))["status"] == "pending"
        again = await _spawn(bridge, caller, origin, "W1", plan_id=plan_id)
        assert again["status"] in {"pending", "starting", "running", "succeeded"}, (
            "a retry of the same operation is the same spawn, never refused by the plan"
        )
        with pytest.raises(ToolError, match="isn't accepted|workers of the accepted fleet plan"):
            await _spawn(bridge, caller, origin, "W2", plan_id=plan_id)
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert len(rows) == 1
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_an_expired_or_someone_elses_plan_starts_nothing(j10_root):
    """#1546 review 1: an accepted plan expires; one accepted by another account (or in
    another thread) isn't this conversation's."""
    from sqlalchemy import text

    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        expired = await _accepted(bridge, caller, origin, 1)
        async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
            await session.execute(
                text(
                    "UPDATE fleet_plans SET expires_at = now() - interval '1 minute' WHERE id = :i"
                ),
                {"i": expired},
            )
        with pytest.raises(ToolError, match="isn't accepted"):
            await _spawn(bridge, caller, origin, "W1", plan_id=expired)
        theirs = await _accepted(bridge, caller, origin, 1)
        async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
            await session.execute(
                text("UPDATE fleet_plans SET thread_id = 'another-thread' WHERE id = :i"),
                {"i": theirs},
            )
        with pytest.raises(ToolError, match="isn't accepted"):
            await _spawn(bridge, caller, origin, "W2", plan_id=theirs)
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert not rows
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_two_taps_at_once_accept_one_plan(j10_root):
    """#1546 review: a concurrent double accept accepts exactly one (a row lock)."""
    import asyncio
    import uuid

    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        made = dict(
            await bridge.plan(
                caller, workers=[PlanWorker(role="w", task="t")], origin_context_id=origin.id
            )
        )
        plan_id = uuid.UUID(str(made["plan_id"]))
        async with bridge._plan_sessions()() as session:  # pyright: ignore[reportPrivateUsage]
            plan, _ = await pending_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                now=datetime.now(UTC),
            )

        async def tap() -> bool:
            async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
                return await accept_plan(
                    session,
                    plan_id=plan_id,
                    tenant_id=caller.tenant_id,
                    account_id=caller.account_id,
                    plan=plan,
                    now=datetime.now(UTC),
                    ttl=timedelta(minutes=30),
                )

        assert sorted(await asyncio.gather(tap(), tap())) == [False, True]
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_accepting_rechecks_the_stored_caps_and_workers(j10_root):
    """#1546 review 4: the plan a caller hands to accept is checked against what was
    stored, so it can't widen the caps or swap the workers."""
    import uuid

    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        made = dict(
            await bridge.plan(
                caller, workers=[PlanWorker(role="w", task="t")], origin_context_id=origin.id
            )
        )
        plan_id = uuid.UUID(str(made["plan_id"]))
        async with bridge._plan_sessions().begin() as session:  # pyright: ignore[reportPrivateUsage]
            plan, caps = await pending_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                now=datetime.now(UTC),
            )
            wider = plan.model_copy(update={"budget_usd": caps.budget_usd * 10})
            with pytest.raises(PlanRefused, match="more than this fleet's"):
                await accept_plan(
                    session,
                    plan_id=plan_id,
                    tenant_id=caller.tenant_id,
                    account_id=caller.account_id,
                    plan=wider,
                    now=datetime.now(UTC),
                    ttl=timedelta(minutes=30),
                )
            swapped = plan.model_copy(
                update={
                    "workers": (
                        *plan.workers,
                        PlannedWorker(role="x", task="t", model=plan.workers[0].model),
                    )
                }
            )
            with pytest.raises(PlanRefused, match="workers are fixed"):
                await accept_plan(
                    session,
                    plan_id=plan_id,
                    tenant_id=caller.tenant_id,
                    account_id=caller.account_id,
                    plan=swapped,
                    now=datetime.now(UTC),
                    ttl=timedelta(minutes=30),
                )
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_j10_a_clone_or_a_reviewer_is_checked_against_the_plan(j10_root):
    """#1546 review 2 and 5: a clone under a plan names its model; a reviewer spends from
    its run's plan (and a run started without one can't be reviewed under one)."""
    import uuid

    from mux.contracts.ids import ResourceRef

    origin_cm, origin, root, ports, bridge, caller = await _fleet(j10_root, require_plan=True)
    try:
        plan_id = await _accepted(bridge, caller, origin, 1)
        clone = ResourceRef.model_validate(
            {
                "tenant_id": str(caller.tenant_id),
                "provider": "anthropic",
                "kind": "agent",
                "id": "some-agent",
                "account_id": str(caller.account_id),
                "account_scope_id": "default",
            }
        )
        with pytest.raises(ToolError, match="names its model"):
            await bridge.spawn(
                caller,
                run_id=None,
                origin_context_id=origin.id,
                operation_key="C1",
                role="clone",
                backend=None,
                agent=None,
                clone=clone,
                clone_revision=None,
                overrides=None,
                session=None,
                budget_usd=None,
                deadline=None,
                max_turns=None,
                result_contract=None,
                reviewer_of=None,
                plan_id=plan_id,
            )
        with pytest.raises(ToolError, match="wasn't started under an accepted plan"):
            await _spawn(
                bridge,
                caller,
                origin,
                "R1",
                # This turn's run, started without a plan.
                reviewer_of=uuid.uuid4(),
                budget_usd=Decimal("0.01"),
            )
    finally:
        await ports.close()
        await origin_cm.__aexit__(None, None, None)


async def test_with_plans_off_the_tools_are_unchanged_and_routines_never_need_one():
    """#1546 review 3 and 7: with plans off there's no fleet_plan tool and no plan_id
    on fleet_spawn. With them on, only a person's chat turn needs a plan: agent keys and
    the hub carry no chat credential, and routine turns keep no fleet root at all."""
    from daimon.adapters.mcp.tools.fleet import register_fleet_tools
    from fastmcp import FastMCP

    for on in (False, True):
        base = runtime()
        settings = base.settings.model_copy(update={"fleet": FleetSettings(require_plan=on)})
        mcp = FastMCP("guard")
        register_fleet_tools(mcp, replace(base, settings=settings), enabled=True)
        names = {tool.name for tool in await mcp.list_tools()}
        spawn = await mcp.get_tool("fleet_spawn")
        assert ("fleet_plan" in names) is on
        assert ("plan_id" in (spawn.parameters.get("properties") or {})) is on
        bridge = HostFleetToolService(
            FleetOrchestrator(None),  # type: ignore[arg-type]
            runtime=replace(base, settings=settings),
        )
        headless = replace(AUTH, chat_agent_id=None)
        assert bridge._plan_required(headless) is False, "no chat credential: never a plan"  # pyright: ignore[reportPrivateUsage]
        chat = replace(AUTH, chat_agent_id=AUTH.account_id, agent_id=None)
        assert bridge._plan_required(chat) is on  # pyright: ignore[reportPrivateUsage]
        key = replace(chat, agent_id=AUTH.account_id)
        assert bridge._plan_required(key) is False, "an agent key never needs a plan"  # pyright: ignore[reportPrivateUsage]
