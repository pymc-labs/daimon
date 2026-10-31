"""The headless runner (routine fires, smoke, `daimon run`) interrupted at each recovery point.

A headless turn opens a fresh provider session per call, so "restart" means the
next call: a new process runs the next fire. Nothing replays the interrupted
one on either path; the cells record what the interruption left behind.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import pytest
from daimon.core import headless_runner
from daimon.core.pricing import MODEL_PRICING
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.state import TurnState
from daimon.core.usage_recording import record_turn_usage
from daimon.testing.recovery import Process, ledger, record

from .conftest import Scenario

Mode = Literal["kill", "cancel"]
MODES: tuple[Mode, ...] = ("kill", "cancel")


def usage_factory(process: Process, scenario: Scenario) -> Callable[[str, str], Callable[..., Any]]:
    """The recorder the scheduler binds for a fire (`daimon.adapters.scheduler.main`)."""

    def factory(session_id: str, model_id: str) -> Callable[..., Any]:
        return functools.partial(
            record_turn_usage,
            sessionmaker=process.sessionmaker,
            platform_user_id="creator",
            managed_session_id=session_id,
            model_id=model_id,
            tenant_id=scenario.tenant_id,
            markup=Decimal(1),
            pricing=MODEL_PRICING.get(model_id),
            channel_id=None,
        )

    return factory


async def headless(
    process: Process,
    scenario: Scenario,
    text: str,
    *,
    on_state: Callable[[TurnState], None] | None = None,
) -> str:
    return await headless_runner.run_turn(
        anthropic=process.deps.anthropic,
        agent_id="ag_recovery",
        environment_id="env_recovery",
        trigger_message=text,
        tenant_id=scenario.tenant_id,
        account_id=scenario.account_id,
        session_factory=process.sessionmaker,
        usage_record_factory=usage_factory(process, scenario),
        turn_path=process.path,
        on_state=on_state,
        tool_safety=OPEN_TOOL_SAFETY,
    )


async def interrupt(process: Process, mode: Mode) -> None:
    await (process.kill() if mode == "kill" else process.cancel())


async def interrupted_then_next_fire(
    scenario: Scenario, reached: asyncio.Event, mode: Mode, *, settle_s: float = 0.0
) -> tuple[Any, str | None, str | None]:
    task = scenario.process.spawn(headless(scenario.process, scenario, "first fire"))
    await asyncio.wait_for(reached.wait(), timeout=20)
    await asyncio.sleep(settle_s)
    await interrupt(scenario.process, mode)
    before = await ledger(scenario.engine, scenario.tenant_id)
    process = scenario.restart()
    try:
        reply = await asyncio.wait_for(headless(process, scenario, "next fire"), timeout=30)
        error = None
    except Exception as err:  # the next fire's own failure is part of the observation
        reply, error = None, f"{type(err).__name__}: {err}"
    assert task.done()
    return before, reply, error


def observation(scenario: Scenario, before: Any, after: Any, reply: str | None, error: str | None):
    world = scenario.world
    calls = world.model_calls
    return {
        "provider_inputs_accepted": [t for _, t in world.accepted_inputs],
        "provider_sessions_created": len(world.created_sessions),
        "provider_model_calls": len(calls),
        "debits": after.debits,
        "billed_model_calls": sum(1 for c in calls if after.usage_events.get(c, 0) >= 1),
        "max_debit_rows_per_call": max(after.usage_events.values(), default=0),
        "leases_held_after_death": before.live_leases,
        "operations_after_death": before.operations_by_status,
        "operations_after_next_fire": after.operations_by_status,
        "next_fire_reply": reply,
        "next_fire_error": error,
    }


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("create_fault", ["hang", "accept_hang"])
async def test_h1_before_session_creation(
    scenario: Scenario, mode: Mode, create_fault: Literal["hang", "accept_hang"]
) -> None:
    scenario.world.create_faults.append(create_fault)
    before, reply, error = await interrupted_then_next_fire(
        scenario, scenario.world.reached_create.event, mode
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert error is None and reply == "reply to: next fire"
    assert [t for _, t in world.accepted_inputs] == ["next fire"]
    assert len(world.created_sessions) == (1 if create_fault == "hang" else 2)
    assert after.live_leases == 0 and after.operations_by_status == {}
    point = "H1 before session creation" + (
        " (request lost)" if create_fault == "hang" else " (session created, response lost)"
    )
    record(
        "headless", point, mode, scenario.path, **observation(scenario, before, after, reply, error)
    )


@pytest.mark.parametrize("mode", MODES)
async def test_h2_input_accepted_but_receipt_never_arrives(scenario: Scenario, mode: Mode) -> None:
    scenario.world.send_faults.append("accept_hang")
    before, reply, error = await interrupted_then_next_fire(
        scenario, scenario.world.reached_send.event, mode
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert error is None
    # A fire is a fresh session each time: the accepted input is never sent again.
    assert [t for _, t in world.accepted_inputs] == ["first fire", "next fire"]
    assert len(world.created_sessions) == 2
    assert after.live_leases == 0 and after.operations_by_status == {}
    record(
        "headless",
        "H2 input accepted, receipt lost",
        mode,
        scenario.path,
        **observation(scenario, before, after, reply, error),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_h3_interrupted_while_waiting_out_a_retryable_refusal(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.send_faults.append("refuse_429")
    before, reply, error = await interrupted_then_next_fire(
        scenario, scenario.world.reached_refusal.event, mode, settle_s=0.4
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert error is None
    assert [t for _, t in world.accepted_inputs] == ["next fire"], "the refused input never ran"
    record(
        "headless",
        "H3 during retryable refusal",
        mode,
        scenario.path,
        **observation(scenario, before, after, reply, error),
        provider_send_posts=world.posts("/events"),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_h4_provider_finished_but_process_never_saw_it(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.stream_faults.append("hang_before_span")
    before, reply, error = await interrupted_then_next_fire(
        scenario, scenario.world.reached_stream_hang.event, mode
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert error is None
    assert len(world.model_calls) == 2
    record(
        "headless",
        "H4 completed at provider, unseen",
        mode,
        scenario.path,
        **observation(scenario, before, after, reply, error),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_h5_after_the_answer_before_the_caller_commits_it(
    scenario: Scenario, mode: Mode
) -> None:
    reached = asyncio.Event()
    # The caller's commit is the scheduler's `record_result`; here the runner's own hand-back
    # point stands in for it: the answer exists, the caller dies before it can store it.
    task = scenario.process.spawn(_headless_then_stall(scenario.process, scenario, reached))
    await asyncio.wait_for(reached.wait(), timeout=20)
    await interrupt(scenario.process, mode)
    assert task.done()
    before = await ledger(scenario.engine, scenario.tenant_id)
    process = scenario.restart()
    reply = await asyncio.wait_for(headless(process, scenario, "next fire"), timeout=30)
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert reply == "reply to: next fire"
    assert [t for _, t in world.accepted_inputs] == ["first fire", "next fire"]
    assert before.usage_events == {world.model_calls[0]: 1}, "the answer was billed as it streamed"
    record(
        "headless",
        "H5 answer in hand, caller dies before committing it",
        mode,
        scenario.path,
        **observation(scenario, before, after, reply, None),
    )


async def _headless_then_stall(
    process: Process, scenario: Scenario, reached: asyncio.Event
) -> None:
    await headless(process, scenario, "first fire")
    reached.set()
    await asyncio.Event().wait()


async def test_h2x_connection_drops_after_the_provider_accepted_the_input(
    scenario: Scenario,
) -> None:
    """No process dies: the answer to the send is lost."""
    scenario.world.send_faults.append("accept_drop")
    reply, error = None, None
    try:
        reply = await asyncio.wait_for(headless(scenario.process, scenario, "only fire"), 30)
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    inputs = [t for _, t in world.accepted_inputs]
    assert inputs == ["only fire"], "the send is one attempt; an unknown delivery is not resent"
    assert after.debits == 1
    record(
        "headless",
        "H2x connection drop after acceptance (no restart)",
        "none",
        scenario.path,
        provider_inputs_accepted=inputs,
        provider_model_calls=len(world.model_calls),
        next_fire_reply=reply,
        next_fire_error=error,
        debits=after.debits,
    )
