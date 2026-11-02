"""A chat turn through the core pipeline, interrupted at each recovery point.

Every cell runs on `legacy` and `mux` (the `turn_path` fixture) and in two
modes: `kill` (SIGKILL: nothing in the dying process can write) and `cancel`
(a drain that cancelled the task and let its cleanup run). After the
interruption a fresh process takes the same user's retry on the same thread.
The tests assert the invariants that must hold on both paths and record the
rest for MATRIX.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import pytest
from daimon.core.turn.state import extract_final_response
from daimon.testing.recovery import Ledger, Process, count, ledger, record
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy import text

from .conftest import Scenario
from .support import start_chat

Mode = Literal["kill", "cancel"]
MODES: tuple[Mode, ...] = ("kill", "cancel")

#: How long a retry may take before it is called blocked; a free thread answers in well under 1s.
PATIENCE_S = 3.0


async def interrupt(process: Process, mode: Mode) -> None:
    await (process.kill() if mode == "kill" else process.cancel())


async def expire_leases(scenario: Scenario) -> int:
    """Move every held lease past its expiry, as five minutes of wall clock would."""
    async with scenario.engine.begin() as connection:
        result = await connection.execute(
            text(
                "update thread_lease set active = jsonb_set(active, '{expires_at}', "
                "to_jsonb(to_char((now() at time zone 'utc') - interval '1 second', "
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"\'))) '
                "where jsonb_typeof(active) = 'object'"
            )
        )
        return result.rowcount


@dataclass
class Retry:
    """What the user's retry on a fresh process did."""

    blocked_by_lease: bool
    lease_seconds_left_when_blocked: int | None
    renders_while_blocked: int
    reply: str | None
    error: str | None
    seconds_to_first_result: float
    lifecycle: RecordingLifecycle


async def retry_turn(scenario: Scenario, text_: str = "second question") -> Retry:
    """Run the user's retry on a restarted process; free an expired lease if it waits on one."""
    process = scenario.restart()
    turn, task = start_chat(process, scenario.tenant_id, text_)
    loop = asyncio.get_running_loop()
    started = loop.time()
    done, _ = await asyncio.wait({task}, timeout=PATIENCE_S)
    blocked = not done
    renders_while_blocked = len(turn.lifecycle.renders) + len(turn.lifecycle.terminal_failures)
    left: int | None = None
    if blocked:
        left = await count(
            scenario.engine,
            "select coalesce(max(extract(epoch from (active->>'expires_at')::timestamptz "
            "- now())), 0)::int from thread_lease where jsonb_typeof(active) = 'object'",
        )
        await expire_leases(scenario)
        await asyncio.wait_for(task, timeout=30)
    elapsed = loop.time() - started
    outcome = task.result()
    return Retry(
        blocked_by_lease=blocked,
        lease_seconds_left_when_blocked=left,
        renders_while_blocked=renders_while_blocked,
        reply=extract_final_response(outcome.state.content) or None,
        error=str(outcome.state.error) if outcome.state.error is not None else None,
        seconds_to_first_result=round(elapsed, 2),
        lifecycle=turn.lifecycle,
    )


def summarize(scenario: Scenario, before: Ledger, after: Ledger, retry: Retry) -> dict[str, object]:
    world = scenario.world
    calls = world.model_calls
    return {
        "provider_inputs_accepted": [text_ for _, text_ in world.accepted_inputs],
        "provider_sessions_created": len(world.created_sessions),
        "provider_model_calls": len(calls),
        "debits": after.debits,
        "billed_model_calls": sum(1 for c in calls if after.usage_events.get(c, 0) >= 1),
        "max_debit_rows_per_call": max(after.usage_events.values(), default=0),
        "leases_held_after_death": before.live_leases,
        "operations_after_death": before.operations_by_status,
        "retry_blocked_by_lease": retry.blocked_by_lease,
        "lease_seconds_left_when_blocked": retry.lease_seconds_left_when_blocked,
        "retry_card_updates_while_blocked": retry.renders_while_blocked,
        "retry_reply": retry.reply,
        "retry_error": retry.error,
        "retry_seconds": retry.seconds_to_first_result,
        "leases_held_after_retry": after.live_leases,
        "operations_after_retry": after.operations_by_status,
        "active_turn_markers_after_retry": after.active_turn_markers,
        "open_turn_outcomes_after_death": before.open_turn_outcomes,
    }


async def interrupted_then_retried(
    scenario: Scenario,
    point: str,
    mode: Mode,
    reached: asyncio.Event,
    *,
    text_: str = "first question",
    settle_s: float = 0.0,
) -> tuple[Ledger, Retry, RecordingLifecycle]:
    """Interrupt the first turn at `reached` (after `settle_s` for the state to be written), then retry."""
    first = RecordingLifecycle()
    _, task = start_chat(scenario.process, scenario.tenant_id, text_, first)
    await asyncio.wait_for(reached.wait(), timeout=20)
    await asyncio.sleep(settle_s)
    await interrupt(scenario.process, mode)
    assert task.done(), "the interrupted turn must be gone"
    before = await ledger(scenario.engine, scenario.tenant_id)
    retry = await retry_turn(scenario)
    return before, retry, first


@pytest.mark.parametrize("mode", MODES)
async def test_c1_before_session_creation_provider_never_saw_it(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.create_faults.append("hang")
    before, retry, first = await interrupted_then_retried(
        scenario, "c1", mode, scenario.world.reached_create.event
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None and retry.reply == "reply to: second question"
    assert [t for _, t in world.accepted_inputs] == ["second question"]
    assert len(world.created_sessions) == 1
    assert max(after.usage_events.values(), default=0) <= 1
    assert after.live_leases == 0
    record(
        "chat",
        "C1 before session creation (request lost)",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
        interrupted_turn_rendered=len(first.renders),
        interrupted_turn_failures=len(first.terminal_failures),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_c1b_session_created_but_process_dies_before_it_hears(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.create_faults.append("accept_hang")
    before, retry, _first = await interrupted_then_retried(
        scenario, "c1b", mode, scenario.world.reached_create.event
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None
    assert [t for _, t in world.accepted_inputs] == ["second question"]
    assert len(world.created_sessions) == 2, "the first session is an orphan the provider keeps"
    assert after.live_leases == 0
    record(
        "chat",
        "C1b session created, response lost",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_c2_input_accepted_but_receipt_never_arrives(scenario: Scenario, mode: Mode) -> None:
    scenario.world.send_faults.append("accept_hang")
    before, retry, _first = await interrupted_then_retried(
        scenario, "c2", mode, scenario.world.reached_send.event
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None
    # The user's own retry is a new input: the provider has the first one and this one.
    assert [t for _, t in world.accepted_inputs] == ["first question", "second question"]
    assert max(after.usage_events.values(), default=0) <= 1
    record(
        "chat",
        "C2 input accepted, receipt lost",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
    )


async def test_c2x_connection_drops_after_the_provider_accepted_the_input(
    scenario: Scenario,
) -> None:
    """No process dies: the answer to the send is lost and the SDK has retries left."""
    scenario.world.send_faults.append("accept_drop")
    _turn, task = start_chat(scenario.process, scenario.tenant_id, "only question")
    outcome = await asyncio.wait_for(task, timeout=30)
    world = scenario.world
    after = await ledger(scenario.engine, scenario.tenant_id)
    inputs = [t for _, t in world.accepted_inputs]
    if scenario.path == "mux":
        assert inputs == ["only question"], "mux sends once; an unknown delivery is not resent"
        assert outcome.state.error is None, "the driver reattaches and reads the answer back"
        assert after.debits == 1
    else:
        assert inputs == ["only question", "only question"], "legacy's SDK resends the POST"
        assert outcome.state.error is None
        assert after.debits == 2, "and the duplicate input is a second model call to pay for"
    record(
        "chat",
        "C2x connection drop after acceptance (no restart)",
        "none",
        scenario.path,
        provider_inputs_accepted=inputs,
        provider_model_calls=len(world.model_calls),
        turn_error=str(outcome.state.error) if outcome.state.error else None,
        turn_reply=extract_final_response(outcome.state.content) or None,
        debits=after.debits,
        operations_after=after.operations_by_status,
        leases_held_after=after.live_leases,
    )


@pytest.mark.parametrize("mode", MODES)
async def test_c3_interrupted_while_waiting_out_a_retryable_refusal(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.send_faults.append("refuse_429")
    before, retry, _first = await interrupted_then_retried(
        scenario, "c3", mode, scenario.world.reached_refusal.event, settle_s=0.4
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None
    assert [t for _, t in world.accepted_inputs] == ["second question"], (
        f"a refused input never ran, and the interrupted turn must not resend it: {world.requests}"
    )
    record(
        "chat",
        "C3 during retryable refusal",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
        provider_send_posts=world.posts("/events"),
    )


@pytest.mark.parametrize("mode", MODES)
async def test_c4_provider_finished_but_process_never_saw_it(
    scenario: Scenario, mode: Mode
) -> None:
    scenario.world.stream_faults.append("hang_before_span")
    before, retry, _first = await interrupted_then_retried(
        scenario, "c4", mode, scenario.world.reached_stream_hang.event
    )
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None
    assert len(world.model_calls) == 2
    record(
        "chat",
        "C4 completed at provider, unseen",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
    )


class StallingLifecycle(RecordingLifecycle):
    """Delivery that never finishes: the process dies inside `on_terminal_success`."""

    def __init__(self) -> None:
        super().__init__()
        self.reached = asyncio.Event()

    async def on_terminal_success(self, state) -> None:  # type: ignore[no-untyped-def]
        self.reached.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("mode", MODES)
async def test_c5_before_platform_delivery(scenario: Scenario, mode: Mode) -> None:
    stalling = StallingLifecycle()
    _, task = start_chat(scenario.process, scenario.tenant_id, "first question", stalling)
    await asyncio.wait_for(stalling.reached.wait(), timeout=20)
    await interrupt(scenario.process, mode)
    assert task.done()
    before = await ledger(scenario.engine, scenario.tenant_id)
    retry = await retry_turn(scenario)
    after = await ledger(scenario.engine, scenario.tenant_id)
    world = scenario.world
    assert retry.error is None
    assert before.usage_events == {world.model_calls[0]: 1}, "the answer was billed before delivery"
    assert [t for _, t in world.accepted_inputs] == ["first question", "second question"]
    record(
        "chat",
        "C5 before platform delivery",
        mode,
        scenario.path,
        **summarize(scenario, before, after, retry),
    )


async def lease_seconds_left(scenario: Scenario) -> int:
    return await count(
        scenario.engine,
        "select coalesce(max(extract(epoch from (active->>'expires_at')::timestamptz "
        "- now())), 0)::int from thread_lease where jsonb_typeof(active) = 'object'",
    )


async def set_lease_expiry_in(scenario: Scenario, seconds: float) -> None:
    async with scenario.engine.begin() as connection:
        await connection.execute(
            text(
                "update thread_lease set active = jsonb_set(active, '{expires_at}', "
                "to_jsonb(to_char((now() at time zone 'utc') + make_interval(secs => :s), "
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'))) where jsonb_typeof(active) = 'object'"
            ),
            {"s": seconds},
        )


@pytest.mark.parametrize("point", ["accepted", "refused", "unseen"])
async def test_reg1_a_retry_after_sigkill_waits_at_most_the_lease_ttl(
    scenario: Scenario, point: str
) -> None:
    """Legacy answers at once. Mux strands the thread for at most the lease TTL, then proceeds.

    The wait cannot be run for real here, so the bound is checked in two halves: the lease a
    dead holder leaves expires within `LEASE_TTL` (read from the database), and a waiter takes
    over within one poll of that expiry.
    """
    from daimon.core.turn.persistence import LEASE_TTL, LeaseWait

    world = scenario.world
    if point == "accepted":
        world.send_faults.append("accept_hang")
        reached = world.reached_send.event
    elif point == "refused":
        world.send_faults.append("refuse_429")
        reached = world.reached_refusal.event
    else:
        world.stream_faults.append("hang_before_span")
        reached = world.reached_stream_hang.event
    _, task = start_chat(scenario.process, scenario.tenant_id, "first question")
    await asyncio.wait_for(reached.wait(), timeout=20)
    await asyncio.sleep(0.4)
    await scenario.process.kill()
    assert task.done()
    process = scenario.restart()
    turn, retry = start_chat(process, scenario.tenant_id, "second question")
    loop = asyncio.get_running_loop()
    started = loop.time()
    if scenario.path == "legacy":
        await asyncio.wait_for(retry, timeout=PATIENCE_S)
        return
    done, _ = await asyncio.wait({retry}, timeout=1.0)
    assert not done, "the dead holder's lease is still in force"
    assert 0 < await lease_seconds_left(scenario) <= LEASE_TTL.total_seconds()
    set_at = loop.time()
    await set_lease_expiry_in(scenario, 2.0)
    outcome = await asyncio.wait_for(retry, timeout=2.0 + LeaseWait(1).poll_max_s + 3.0)
    assert outcome.state.error is None
    assert loop.time() - set_at <= 2.0 + LeaseWait(1).poll_max_s + 1.5, (
        "a waiter proceeds within one poll of the lease lapsing"
    )
    assert loop.time() - started < LEASE_TTL.total_seconds()
    del turn
