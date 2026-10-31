"""A scheduler routine fire interrupted at each recovery point, then restarted.

The real `run_one_tick` claims the slot and the real `_build_fire` runs the
fire against a provider that outlives the process (`daimon.testing.recovery`).
`kill` is SIGKILL (the process's database handle dies with it); `cancel` is the
scheduler's graceful shutdown (`RoutineDispatcher.close`). After the
interruption a fresh process ticks again: the same slot first, then the next.
"""

from __future__ import annotations

import asyncio
import os
import unittest.mock
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from daimon.adapters.scheduler.main import (
    _build_fire,  # pyright: ignore[reportPrivateUsage]
    _CapsAdapter,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.routine_delivery import DeliveryOutcome, poll_deliveries_once
from daimon.core.scheduler import RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import claim_routine_deliveries, create_routine, get_routine
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.recovery import (
    KillableSessionmaker,
    ProviderWorld,
    ledger,
    record,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

Mode = Literal["kill", "cancel"]
MODES: tuple[Mode, ...] = ("kill", "cancel")
NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
TICK = dict(max_age=timedelta(minutes=15), max_concurrent_fires=4, dispatch_timeout_s=60.0)


@pytest.fixture(params=["legacy", "mux"])
def turn_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DAIMON_TURN__PATH", request.param)
    return str(request.param)


def make_settings(monkeypatch: pytest.MonkeyPatch, path: str) -> Settings:
    for name in list(os.environ):
        if name.startswith("DAIMON_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("DAIMON_TURN__PATH", path)
    return settings


@dataclass
class Fleet:
    """The scheduler service across restarts: one provider, one database, many processes."""

    world: ProviderWorld
    engine: AsyncEngine
    settings: Settings
    tenant_id: Any
    routine: RoutineRow
    path: str
    sessionmaker: KillableSessionmaker | None = None
    dispatcher: RoutineDispatcher | None = None

    async def tick(self, at: datetime) -> RoutineDispatcher:
        """Tick on this process (starting it on first use); the fire runs in the background."""
        if self.sessionmaker is None:
            self.sessionmaker = KillableSessionmaker(self.engine, expire_on_commit=False)
        sm = self.sessionmaker
        fire = await _build_fire(
            client=self.world.client(),
            sm=sm,
            settings=self.settings,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )
        self.dispatcher = await run_one_tick(
            now=at,
            sm=sm,
            caps=_CapsAdapter(sm, billing_config=None),
            fire=fire,
            dispatcher=self.dispatcher,
            **TICK,  # type: ignore[arg-type]
        )
        return self.dispatcher

    async def interrupt(self, mode: Mode) -> None:
        assert self.dispatcher is not None and self.sessionmaker is not None
        if mode == "kill":
            self.sessionmaker.dead = True
            tasks = list(self.dispatcher._tasks.values())  # pyright: ignore[reportPrivateUsage]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            await self.dispatcher.close()

    def restart(self) -> None:
        """A new scheduler process: new session factory, client, dispatcher and caches."""
        self.sessionmaker = None
        self.dispatcher = None

    async def drain(self) -> None:
        if self.dispatcher is not None:
            await asyncio.wait_for(self.dispatcher.drain(), timeout=30)

    async def row(self) -> RoutineRow:
        sm = KillableSessionmaker(self.engine, expire_on_commit=False)
        async with sm() as s:
            row = await get_routine(s, self.routine.id, tenant_id=self.tenant_id)
        assert row is not None
        return row


@pytest.fixture
async def fleet(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    turn_path: str,
):
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    routine = await create_routine(
        db_session,
        created_by_user_id="U_OWNER",
        agent_id="ag_recovery",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="weekly digest",
        next_fire_at=NOW - timedelta(minutes=1),
        tenant_id=tenant.id,
        destination_kind="channel",
        destination_id="111222333",
    )
    await db_session.commit()
    world = ProviderWorld(
        agent=ma_agent(id="ag_recovery", name="daimon", tenant_id=tenant.id),
        environment=ma_environment(id="env_recovery", name="default", tenant_id=tenant.id),
        tenant_id=tenant.id,
    )
    settings = make_settings(monkeypatch, turn_path)

    async def resolved(*_args: object, **_kwargs: object) -> str:
        return "ag_recovery"

    async def resolved_env(*_args: object, **_kwargs: object) -> str:
        return "env_recovery"

    with (
        unittest.mock.patch("daimon.adapters.scheduler.main.resolve_agent", side_effect=resolved),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment", side_effect=resolved_env
        ),
    ):
        yield Fleet(
            world=world,
            engine=db_committing_engine,
            settings=settings,
            tenant_id=tenant.id,
            routine=routine,
            path=turn_path,
        )


def routine_state(row: RoutineRow) -> dict[str, object]:
    return {
        "last_error": row.last_error,
        "has_result": row.last_result_tail is not None,
        "delivery_status": row.delivery_status,
        "delivery_note": row.delivery_note,
        "next_fire_at": row.next_fire_at.isoformat() if row.next_fire_at else None,
    }


async def run_to(fleet: Fleet, reached: asyncio.Event, mode: Mode, *, settle_s: float = 0.0):
    """Tick, wait for the fire to reach the fault, interrupt, and read what it left."""
    await fleet.tick(NOW)
    try:
        await asyncio.wait_for(reached.wait(), timeout=20)
    except TimeoutError:
        row = await fleet.row()
        raise AssertionError(f"the fire never reached the fault: {routine_state(row)}") from None
    await asyncio.sleep(settle_s)
    await fleet.interrupt(mode)
    return await fleet.row(), await ledger(fleet.engine, fleet.tenant_id)


async def restart_and_tick_twice(fleet: Fleet):
    """New process: the same slot (already claimed), then the next slot."""
    fleet.restart()
    await fleet.tick(NOW + timedelta(seconds=30))
    await fleet.drain()
    same_slot_inputs = [t for _, t in fleet.world.accepted_inputs]
    await fleet.tick(NOW + timedelta(minutes=2))
    await fleet.drain()
    return same_slot_inputs, await fleet.row(), await ledger(fleet.engine, fleet.tenant_id)


def summarize(
    fleet, dead_row, dead_ledger, same_slot_inputs, after_row, after
) -> dict[str, object]:
    world = fleet.world
    calls = world.model_calls
    return {
        "after_death_routine": routine_state(dead_row),
        "provider_inputs_after_restart_same_slot": same_slot_inputs,
        "provider_inputs_accepted": [
            t.split("</turn_controls>\n")[-1] for _, t in world.accepted_inputs
        ],
        "provider_sessions_created": len(world.created_sessions),
        "provider_model_calls": len(calls),
        "debits": after.debits,
        "billed_model_calls": sum(1 for c in calls if after.usage_events.get(c, 0) >= 1),
        "max_debit_rows_per_call": max(after.usage_events.values(), default=0),
        "leases_held_after_death": dead_ledger.live_leases,
        "operations_after_death": dead_ledger.operations_by_status,
        "after_next_slot_routine": routine_state(after_row),
    }


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("create_fault", ["hang", "accept_hang"])
async def test_r1_before_session_creation(
    fleet: Fleet, mode: Mode, create_fault: Literal["hang", "accept_hang"]
) -> None:
    fleet.world.create_faults.append(create_fault)
    dead_row, dead = await run_to(fleet, fleet.world.reached_create.event, mode)
    same, after_row, after = await restart_and_tick_twice(fleet)
    assert same == [], "the claimed slot is not fired again by the restarted process"
    assert len(fleet.world.accepted_inputs) == 1, "only the next slot's fire reached the provider"
    assert after_row.last_error is None and after_row.delivery_status == "pending"
    point = "R1 before session creation" + (
        " (request lost)" if create_fault == "hang" else " (session created, response lost)"
    )
    record(
        "routine",
        point,
        mode,
        fleet.path,
        **summarize(fleet, dead_row, dead, same, after_row, after),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_r2_input_accepted_but_receipt_never_arrives(fleet: Fleet, mode: Mode) -> None:
    fleet.world.send_faults.append("accept_hang")
    dead_row, dead = await run_to(fleet, fleet.world.reached_send.event, mode)
    same, after_row, after = await restart_and_tick_twice(fleet)
    assert same == ["weekly digest"] or len(same) == 1, "only the interrupted fire's own input"
    assert len(fleet.world.accepted_inputs) == 2, "one per slot: the lost one is never resent"
    record(
        "routine",
        "R2 input accepted, receipt lost",
        mode,
        fleet.path,
        **summarize(fleet, dead_row, dead, same, after_row, after),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_r3_interrupted_while_waiting_out_a_retryable_refusal(
    fleet: Fleet, mode: Mode
) -> None:
    fleet.world.send_faults.append("refuse_429")
    dead_row, dead = await run_to(fleet, fleet.world.reached_refusal.event, mode, settle_s=0.4)
    same, after_row, after = await restart_and_tick_twice(fleet)
    assert same == [], "a refused input never ran"
    assert len(fleet.world.accepted_inputs) == 1
    record(
        "routine",
        "R3 during retryable refusal",
        mode,
        fleet.path,
        **summarize(fleet, dead_row, dead, same, after_row, after),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_r4_provider_finished_but_process_never_saw_it(fleet: Fleet, mode: Mode) -> None:
    fleet.world.stream_faults.append("hang_before_span")
    dead_row, dead = await run_to(fleet, fleet.world.reached_stream_hang.event, mode)
    same, after_row, after = await restart_and_tick_twice(fleet)
    assert len(same) == 1 and len(fleet.world.accepted_inputs) == 2
    record(
        "routine",
        "R4 completed at provider, unseen",
        mode,
        fleet.path,
        **summarize(fleet, dead_row, dead, same, after_row, after),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_r5_answer_in_hand_before_the_result_is_committed(
    fleet: Fleet, mode: Mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached = asyncio.Event()
    from daimon.adapters.scheduler import main as scheduler_main

    real_record = scheduler_main.record_result
    stalled = False

    async def stalling_record(*args: Any, **kwargs: Any) -> None:
        nonlocal stalled
        if not stalled and kwargs.get("tail"):
            stalled = True
            reached.set()
            await asyncio.Event().wait()
        await real_record(*args, **kwargs)

    monkeypatch.setattr(scheduler_main, "record_result", stalling_record)
    dead_row, dead = await run_to(fleet, reached, mode)
    same, after_row, after = await restart_and_tick_twice(fleet)
    assert len(same) == 1 and len(fleet.world.accepted_inputs) == 2
    assert dead.usage_events == {fleet.world.model_calls[0]: 1}, "billed as it streamed"
    record(
        "routine",
        "R5a answer in hand, before result commit",
        mode,
        fleet.path,
        **summarize(fleet, dead_row, dead, same, after_row, after),
    )


class Poster:
    """A platform poster that records every post."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def __call__(self, row: RoutineRow) -> DeliveryOutcome:
        self.posted.append(row.delivery_payload or "")
        return DeliveryOutcome(status="delivered")


@pytest.mark.parametrize("mode", MODES)
async def test_r5b_result_committed_process_dies_before_delivery(fleet: Fleet, mode: Mode) -> None:
    await fleet.tick(NOW)
    await fleet.drain()
    committed = await fleet.row()
    assert committed.delivery_status == "pending"
    await fleet.interrupt(mode)
    poster = Poster()
    fleet.restart()
    sm = KillableSessionmaker(fleet.engine, expire_on_commit=False)
    delivered = await poll_deliveries_once(sm, platform="discord", post=poster, now=NOW)
    again = await poll_deliveries_once(sm, platform="discord", post=poster, now=NOW)
    row = await fleet.row()
    assert (delivered, again, len(poster.posted)) == (1, 0, 1)
    assert row.delivery_status == "delivered"
    record(
        "routine",
        "R5b committed, died before delivery",
        mode,
        fleet.path,
        after_death_routine=routine_state(committed),
        deliveries_posted=len(poster.posted),
        after_restart_routine=routine_state(row),
        provider_model_calls=len(fleet.world.model_calls),
        provider_inputs_accepted=[t for _, t in fleet.world.accepted_inputs][:1],
    )


@pytest.mark.parametrize("mode", MODES)
async def test_r5c_died_mid_delivery_is_skipped_not_reposted(fleet: Fleet, mode: Mode) -> None:
    await fleet.tick(NOW)
    await fleet.drain()
    sm = KillableSessionmaker(fleet.engine, expire_on_commit=False)
    async with sm.begin() as s:
        claimed = await claim_routine_deliveries(
            s, platform="discord", owner="dead-owner", now=NOW, lease=timedelta(seconds=30)
        )
    assert len(claimed) == 1
    await fleet.interrupt(mode)  # the poster may have posted; the process died before settling
    fleet.restart()
    poster = Poster()
    handled = await poll_deliveries_once(
        sm, platform="discord", post=poster, now=NOW + timedelta(minutes=5)
    )
    row = await fleet.row()
    assert handled == 0 and poster.posted == []
    assert (row.delivery_status, row.delivery_note) == ("skipped", "interrupted")
    record(
        "routine",
        "R5c died mid-delivery (claim held)",
        mode,
        fleet.path,
        deliveries_posted=len(poster.posted),
        after_restart_routine=routine_state(row),
    )


async def test_r2x_connection_drop_after_acceptance_no_restart(fleet: Fleet) -> None:
    fleet.world.send_faults.append("accept_drop")
    await fleet.tick(NOW)
    await fleet.drain()
    row = await fleet.row()
    after = await ledger(fleet.engine, fleet.tenant_id)
    inputs = [t for _, t in fleet.world.accepted_inputs]
    assert len(inputs) == 1, "the send is one attempt; an unknown delivery is not resent"
    record(
        "routine",
        "R2x connection drop after acceptance (no restart)",
        "none",
        fleet.path,
        provider_inputs_count=len(inputs),
        provider_model_calls=len(fleet.world.model_calls),
        debits=after.debits,
        routine=routine_state(row),
    )
