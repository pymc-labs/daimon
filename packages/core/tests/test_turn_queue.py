"""TurnQueue: slots, the bounded round-robin queue, Stop, max wait.

`formal/turn_queue/` models the same rules; these tests pin the Python.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.core.config import TurnQueueSettings
from daimon.core.turn.slots import holding, release_turn_slot, wait_for_slot
from daimon.core.turn_queue import TurnQueue, TurnTicket, take_queue_window
from structlog.testing import capture_logs

A = uuid.UUID(int=1)
B = uuid.UUID(int=2)
C = uuid.UUID(int=3)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_queue(
    *,
    global_cap: int | None = 2,
    per_tenant: int = 50,
    total: int = 500,
    max_wait_s: float = 300.0,
    clock: Clock | None = None,
) -> TurnQueue:
    return TurnQueue(
        platform="discord",
        global_cap=global_cap,
        max_queued_per_tenant=per_tenant,
        max_queued=total,
        max_wait_s=max_wait_s,
        clock=clock or Clock(),
    )


def admit(queue: TurnQueue, tenant: uuid.UUID, cap: int = 10) -> TurnTicket:
    ticket = queue.admit(tenant, cap=cap)
    assert ticket is not None
    return ticket


def test_under_both_caps_a_turn_runs_at_once() -> None:
    queue = make_queue()
    ticket = admit(queue, A)
    assert ticket.state == "running"
    assert queue.in_flight() == 1 and queue.in_flight(A) == 1


async def test_over_the_global_cap_turns_queue_and_start_in_order() -> None:
    queue = make_queue(global_cap=2)
    running = [admit(queue, A), admit(queue, A)]
    with capture_logs() as logs:
        waiting = [admit(queue, A) for _ in range(3)]
    assert [t.state for t in waiting] == ["queued"] * 3
    assert [e["position"] for e in logs if e["event"] == "turn.queue.enqueued"] == [1, 2, 3]
    assert queue.depth() == 3

    tasks = [asyncio.create_task(t.wait(asyncio.Event())) for t in waiting]
    await asyncio.sleep(0)
    running[0].release()
    assert [t.state for t in waiting] == ["running", "queued", "queued"]
    running[1].release()
    assert [t.state for t in waiting] == ["running", "running", "queued"]
    waiting[0].release()
    assert [t.state for t in waiting] == ["done", "running", "running"]
    assert await asyncio.gather(*tasks) == ["started"] * 3
    for ticket in waiting:
        ticket.release()
    assert queue.in_flight() == 0 and queue.depth() == 0


def test_over_the_tenant_cap_only_that_tenant_queues() -> None:
    queue = make_queue(global_cap=10)
    admit(queue, A, cap=1)
    assert admit(queue, A, cap=1).state == "queued"
    assert admit(queue, B, cap=1).state == "running"


def test_round_robin_a_flooding_tenant_does_not_starve_another() -> None:
    queue = make_queue(global_cap=1)
    first = admit(queue, A)
    flood = [admit(queue, A) for _ in range(5)]
    other = admit(queue, B)
    assert other.state == "queued"

    first.release()
    assert flood[0].state == "running"  # A was first in rotation
    flood[0].release()
    assert other.state == "running"  # B next, ahead of A's four older turns
    assert [t.state for t in flood[1:]] == ["queued"] * 4


def test_round_robin_over_three_tenants() -> None:
    queue = make_queue(global_cap=1)
    held = admit(queue, A)
    a = [admit(queue, A) for _ in range(2)]
    b = [admit(queue, B) for _ in range(2)]
    c = [admit(queue, C) for _ in range(2)]
    started: list[TurnTicket] = []
    current = held
    for _ in range(6):
        current.release()
        current = next(t for t in [*a, *b, *c] if t.state == "running")
        started.append(current)
    assert started == [a[0], b[0], c[0], a[1], b[1], c[1]]


def test_a_tenant_at_its_cap_does_not_block_others_waiting_on_the_global_cap() -> None:
    queue = make_queue(global_cap=2)
    a_running = admit(queue, A, cap=1)
    b_running = admit(queue, B, cap=2)
    a_waiting = admit(queue, A, cap=1)
    b_waiting = admit(queue, B, cap=2)
    b_running.release()
    # A is first in rotation but still at its own cap: B takes the free slot.
    assert a_waiting.state == "queued"
    assert b_waiting.state == "running"
    a_running.release()
    assert a_waiting.state == "running"


def test_grant_and_pop_happen_in_the_release_span() -> None:
    """No await may sit between starting a waiting turn and popping it.

    `formal/turn_queue` GrantThenPop: a dispatcher that pops after an await
    lets the started turn end before the pop and start a second time. This
    test runs without an event loop step between the release and the checks,
    so it fails if the hand-off ever needs one.
    """
    queue = make_queue(global_cap=1)
    first = admit(queue, A)
    second = admit(queue, A)
    third = admit(queue, A)

    first.release()
    assert second.state == "running"
    assert queue.depth() == 1 and queue.depth(A) == 1
    second.release()  # ends before anything else runs
    assert third.state == "running"
    assert queue.depth() == 0
    third.release()
    assert queue.in_flight() == 0
    # Every ticket started exactly once: nothing is left to start again.
    assert {t.state for t in (first, second, third)} == {"done"}


def test_release_is_idempotent_and_counts_stay_exact() -> None:
    queue = make_queue(global_cap=1)
    first = admit(queue, A)
    second = admit(queue, A)
    first.release()
    first.release()
    assert queue.in_flight() == 1 and second.state == "running"
    second.release()
    second.release()
    assert queue.in_flight() == 0 and queue.in_flight(A) == 0


def test_tenant_queue_full_refuses_with_a_reason() -> None:
    queue = make_queue(global_cap=1, per_tenant=2, total=10)
    admit(queue, A)
    admit(queue, A)
    admit(queue, A)
    with capture_logs() as logs:
        assert queue.admit(A, cap=10, channel_id="c1") is None
    (full,) = [e for e in logs if e["event"] == "turn.queue.full"]
    assert full["reason"] == "tenant_queue_full"
    assert full["channel_id"] == "c1"
    assert queue.depth(A) == 2


def test_global_queue_full_refuses_every_tenant() -> None:
    queue = make_queue(global_cap=1, per_tenant=10, total=2)
    admit(queue, A)
    admit(queue, A)
    admit(queue, B)
    with capture_logs() as logs:
        assert queue.admit(C, cap=10) is None
    assert [e["reason"] for e in logs if e["event"] == "turn.queue.full"] == ["queue_full"]


def test_disabled_queue_refuses_at_the_cap() -> None:
    queue = TurnQueue.from_settings(
        TurnQueueSettings(enabled=False), platform="slack", global_cap=None
    )
    assert queue.admit(A, cap=1) is not None
    assert queue.admit(A, cap=1) is None


async def test_stop_while_queued_removes_the_turn_and_it_never_starts() -> None:
    queue = make_queue(global_cap=1)
    running = admit(queue, A)
    waiting = admit(queue, A)
    behind = admit(queue, A)
    cancel = asyncio.Event()
    task = asyncio.create_task(waiting.wait(cancel))
    await asyncio.sleep(0)
    with capture_logs() as logs:
        cancel.set()
        assert await task == "cancelled"
    assert [e["event"] for e in logs] == ["turn.queue.cancelled"]
    assert queue.depth() == 1
    running.release()
    assert behind.state == "running"
    assert waiting.state == "done"
    waiting.release()  # the adapter's finally: nothing to return
    assert queue.in_flight() == 1


async def test_stop_racing_the_grant_returns_the_slot() -> None:
    queue = make_queue(global_cap=1)
    running = admit(queue, A)
    waiting = admit(queue, A)
    behind = admit(queue, B)
    cancel = asyncio.Event()
    task = asyncio.create_task(waiting.wait(cancel))
    await asyncio.sleep(0)
    # Stop and the grant land before the waiter runs again.
    cancel.set()
    running.release()
    assert waiting.state == "running"
    assert await task == "cancelled"
    waiting.release()
    assert behind.state == "running"
    assert queue.in_flight() == 1


async def test_max_wait_times_out_and_leaves_the_queue() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, max_wait_s=0.01, clock=clock)
    admit(queue, A)
    waiting = admit(queue, A)
    clock.now += 0.01
    with capture_logs() as logs:
        assert await waiting.wait(asyncio.Event()) == "timed_out"
    (timed_out,) = [e for e in logs if e["event"] == "turn.queue.timed_out"]
    assert timed_out["waited_ms"] == 10.0
    assert queue.depth() == 0 and waiting.state == "done"


async def test_a_turn_is_never_dropped_before_the_max_wait() -> None:
    queue = make_queue(global_cap=1, max_wait_s=60)
    running = admit(queue, A)
    waiting = admit(queue, A)
    task = asyncio.create_task(waiting.wait(asyncio.Event()))
    await asyncio.sleep(0.02)
    assert not task.done()
    running.release()
    assert await task == "started"


def test_try_claim_never_queues_or_jumps_the_queue() -> None:
    queue = make_queue(global_cap=2)
    admit(queue, A, cap=1)
    admit(queue, A, cap=1)  # queued behind A's cap
    assert queue.try_claim(A, cap=1) is None
    assert queue.depth() == 1
    assert queue.try_claim(B, cap=1) is not None


def test_a_raised_cap_starts_waiting_turns_before_a_newcomer() -> None:
    queue = make_queue(global_cap=10)
    admit(queue, A, cap=1)
    waiting = admit(queue, A, cap=1)
    newcomer = admit(queue, A, cap=2)
    assert waiting.state == "running"
    assert newcomer.state == "queued"


async def test_started_log_carries_the_wait_and_the_window_reports_percentiles() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, clock=clock)
    queue.take_window()
    running = admit(queue, A)
    waiting = admit(queue, B)
    clock.now += 2.5
    with capture_logs() as logs:
        running.release()
    (started,) = [e for e in logs if e["event"] == "turn.queue.started"]
    assert started["waited_ms"] == 2500.0
    window = queue.take_window()
    assert window["started"] == 1
    assert window["wait_ms_p95"] == 2500.0
    assert window["global_depth"] == 0
    assert queue.take_window()["started"] == 0
    waiting.release()


async def test_wait_for_slot_uses_the_held_ticket() -> None:
    queue = make_queue(global_cap=1)
    sm = MagicMock()
    over_balance = AsyncMock(return_value=False)
    with patch("daimon.core.turn.slots.is_over_balance", over_balance):
        # No ticket held: nothing to wait for.
        assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "started"
        running = admit(queue, A)
        with holding(running):
            assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "started"
            over_balance.assert_not_awaited()  # it never waited: admission's gate stands
            waiting = admit(queue, A)
        with holding(waiting):
            task = asyncio.create_task(wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A))
            await asyncio.sleep(0)
            # The first turn's release handed its slot on.
            assert queue.in_flight() == 1 and waiting.state == "running"
            assert await task == "started"
            over_balance.assert_awaited_once()  # it waited: the balance gate runs again
            assert queue.in_flight() == 1
    assert queue.in_flight() == 0  # holding released the slot


async def test_a_turn_whose_balance_ran_out_while_queued_does_not_start() -> None:
    queue = make_queue(global_cap=1)
    running = admit(queue, A)
    waiting = admit(queue, A)
    with (
        patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=True)),
        holding(waiting),
    ):
        task = asyncio.create_task(
            wait_for_slot(asyncio.Event(), sessionmaker=MagicMock(), tenant_id=A)
        )
        await asyncio.sleep(0)
        running.release()
        assert await task == "balance_depleted"
    assert queue.in_flight() == 0, "the slot it was handed goes back"


async def test_holding_releases_a_queued_ticket_on_error() -> None:
    queue = make_queue(global_cap=1)
    admit(queue, A)
    waiting = admit(queue, A)
    with pytest.raises(RuntimeError), holding(waiting):
        raise RuntimeError
    assert queue.depth() == 0


def test_the_heartbeat_window_reports_depth_and_waits() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, clock=clock)
    take_queue_window()  # drop waits other tests left in live queues
    running = admit(queue, A)
    waiting = [admit(queue, A), admit(queue, B)]
    window = take_queue_window()
    assert window["global_depth"] >= 2 and window["per_tenant_max"] >= 1
    clock.now += 1.5
    running.release()
    window = take_queue_window()
    assert window["started"] >= 1
    assert window["wait_ms_max"] >= 1500.0
    for ticket in waiting:
        ticket.release()


# --- review fixes: overdue at dispatch, fresh ticket per turn, fairness ----


async def test_dispatch_times_out_a_ticket_past_its_max_wait_instead_of_starting_it() -> None:
    """Enqueued at t=0 and the slot frees at t=301 (max wait 300): the turn
    times out; the slot goes to the next waiting turn, which is in time."""
    clock = Clock()
    queue = make_queue(global_cap=1, max_wait_s=300, clock=clock)
    running = admit(queue, A)
    late = admit(queue, A)
    clock.now += 200
    in_time = admit(queue, B)
    clock.now += 101
    with capture_logs() as logs:
        running.release()
    assert late.state == "done", "dispatch expired it rather than granting it"
    assert in_time.state == "running"
    assert await late.wait(asyncio.Event()) == "timed_out"
    (timed_out,) = [e for e in logs if e["event"] == "turn.queue.timed_out"]
    assert timed_out["waited_ms"] == 301_000.0
    assert queue.depth() == 0 and queue.in_flight() == 1


async def test_an_overdue_ticket_wakes_its_waiter_at_once() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, max_wait_s=300, clock=clock)
    running = admit(queue, A)
    late = admit(queue, A)
    waiter = asyncio.create_task(late.wait(asyncio.Event()))
    await asyncio.sleep(0)
    clock.now += 301
    running.release()
    assert await asyncio.wait_for(waiter, 1) == "timed_out"


def test_an_overdue_head_keeps_its_tenant_in_rotation_for_the_next_turn() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, max_wait_s=300, clock=clock)
    running = admit(queue, A)
    late = admit(queue, A)
    clock.now += 299
    fresh = admit(queue, A)
    clock.now += 2
    running.release()
    assert late.state == "done" and fresh.state == "running"


async def test_a_follow_up_after_a_stopped_turn_takes_a_fresh_ticket() -> None:
    """Stop removed the first turn's ticket from the queue; the thread's
    drained follow-up must not inherit that finished ticket."""
    queue = make_queue(global_cap=1)
    running = admit(queue, B)
    first = admit(queue, A)
    sm = MagicMock()
    with (
        patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=False)),
        holding(first),
    ):
        stop = asyncio.Event()
        stop.set()
        assert await wait_for_slot(stop, sessionmaker=sm, tenant_id=A) == "cancelled"
        release_turn_slot()  # the stopped turn ends
        follow_up = asyncio.create_task(
            wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A)
        )
        async with asyncio.timeout(1):
            while not queue.depth(A):
                await asyncio.sleep(0)
        running.release()
        assert await follow_up == "started"
        assert queue.in_flight(A) == 1
    assert queue.in_flight() == 0


async def test_a_follow_up_after_a_timed_out_turn_takes_a_fresh_ticket() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, max_wait_s=300, clock=clock)
    running = admit(queue, B)
    first = admit(queue, A)
    clock.now += 301
    sm = MagicMock()
    with (
        patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=False)),
        holding(first),
    ):
        assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "timed_out"
        release_turn_slot()
        running.release()
        assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "started", (
            "the follow-up waits its own max wait, not the first turn's"
        )
    assert queue.in_flight() == 0


async def test_a_follow_up_in_a_full_queue_is_refused_not_run() -> None:
    queue = make_queue(global_cap=1, per_tenant=0)
    first = admit(queue, A)
    blocker: TurnTicket | None = None
    sm = MagicMock()
    with holding(first):
        assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "started"
        release_turn_slot()
        blocker = admit(queue, B)  # another tenant takes the freed slot
        assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == "queue_full"
    assert blocker.state == "running" and queue.in_flight() == 1


async def test_follow_ups_release_and_re_queue_so_another_tenant_is_not_starved() -> None:
    """Global cap 1: tenant A's thread keeps sending follow-ups while tenant
    B waits. Each A turn returns its slot when it ends, so B runs before A's
    next follow-up instead of timing out behind it."""
    queue = make_queue(global_cap=1)
    sm = MagicMock()
    order: list[str] = []
    first = admit(queue, A)

    async def thread_a() -> None:
        with holding(first):
            for turn in range(3):
                assert await wait_for_slot(asyncio.Event(), sessionmaker=sm, tenant_id=A) == (
                    "started"
                )
                order.append(f"A{turn + 1}")
                await asyncio.sleep(0.01)
                release_turn_slot()

    async def turn_b(ticket: TurnTicket) -> None:
        assert await ticket.wait(asyncio.Event()) == "started"
        order.append("B1")
        await asyncio.sleep(0.01)
        ticket.release()

    with patch("daimon.core.turn.slots.is_over_balance", AsyncMock(return_value=False)):
        a_task = asyncio.create_task(thread_a())
        async with asyncio.timeout(1):
            while not order:
                await asyncio.sleep(0)
        b = admit(queue, B)
        assert b.queued
        await asyncio.wait_for(asyncio.gather(a_task, turn_b(b)), 2)
    assert order == ["A1", "B1", "A2", "A3"]
    assert queue.in_flight() == 0 and queue.depth() == 0


def test_the_wait_sample_is_bounded_without_a_heartbeat() -> None:
    clock = Clock()
    queue = make_queue(global_cap=1, clock=clock)
    holder = admit(queue, A)
    for _ in range(5000):
        waiting = admit(queue, B)
        clock.now += 0.001
        holder.release()
        holder = waiting
    assert len(queue._waits_ms) <= 4096  # pyright: ignore[reportPrivateUsage]
    window = queue.take_window()
    assert window["started"] == 5000, "the started count is exact; only the sample is capped"
    holder.release()
