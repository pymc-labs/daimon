"""A turn that opts into `LeaseWait` queues behind the thread's running turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.persistence import (
    BindingSuperseded,
    LeaseWait,
    LeaseWaitCancelled,
    TurnPersistence,
)
from daimon.testing.factories import make_tenant
from mux.state.lease import Lease
from mux.state.memory import MemoryStateStore
from mux.state.store import StateStore, binding_slot
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .test_persistence import BINDING, NOW, SCOPE, TENANT

TTL = timedelta(minutes=5)


@pytest_asyncio.fixture(params=("memory", "postgres"))
async def stores(
    request: pytest.FixtureRequest, db_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[Callable[[], StateStore]]:
    if request.param == "memory":
        store = MemoryStateStore()
        yield store.restart
    else:
        sessions = async_sessionmaker(db_engine, expire_on_commit=False)
        async with sessions() as db, db.begin():
            await make_tenant(db, id=TENANT, workspace_id="lease-wait")
        yield lambda: PostgresStateStore(sessions, trust_caller_clock=True)


class Pumped:
    """A pump that records that it ran and the lease it ran under."""

    def __init__(self, persistence: TurnPersistence) -> None:
        self.persistence = persistence
        self.lease: Lease | None = None

    async def __call__(self) -> str:
        self.lease = self.persistence._fence()  # pyright: ignore[reportPrivateUsage]
        return "ran"


def quick(timeout_s: float, cancel: asyncio.Event | None = None) -> LeaseWait:
    return LeaseWait(timeout_s=timeout_s, cancel=cancel, poll_initial_s=0.01, poll_max_s=0.02)


def waiter(
    store: StateStore,
    wait: LeaseWait,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
    key: str = "waiter",
) -> tuple[TurnPersistence, Pumped]:
    persistence = TurnPersistence(
        store, BINDING, SCOPE, operation_key=key, now=clock, lease_wait=wait, ttl=TTL
    )
    return persistence, Pumped(persistence)


async def hold(store: StateStore, *, at: datetime = NOW) -> Lease:
    await store.put_binding(BINDING, expected_generation=0)
    return await store.acquire_lease(
        binding_slot(BINDING), holder="holder", turn_id="holder-root", now=at, ttl=TTL
    )


async def holder_still_owns(store: StateStore, lease: Lease) -> bool:
    renewed = await store.renew_lease(lease, now=NOW, ttl=TTL)
    return renewed.fence == lease.fence


async def test_waiter_acquires_after_the_holder_releases(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    held = await hold(store)
    persistence, pump = waiter(store, quick(10))

    task = asyncio.create_task(persistence.run(pump))
    await asyncio.sleep(0.1)
    assert not task.done() and pump.lease is None, "the waiter runs nothing while the lease is held"

    await store.release_lease(held)
    assert await asyncio.wait_for(task, timeout=5) == "ran"
    assert pump.lease is not None and pump.lease.fence == held.fence + 1
    assert persistence.lease_acquired


async def test_waiter_times_out_at_its_bound_and_leaves_the_holder_alone(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    held = await hold(store)
    persistence, pump = waiter(store, quick(0.2))

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await persistence.run(pump)

    assert 0.15 <= asyncio.get_running_loop().time() - started < 3
    assert pump.lease is None and not persistence.lease_acquired
    assert await holder_still_owns(store, held)


async def test_waiter_stopped_while_waiting_releases_nothing_it_does_not_own(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    held = await hold(store)
    cancel = asyncio.Event()
    persistence, pump = waiter(store, quick(10, cancel))

    task = asyncio.create_task(persistence.run(pump))
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(LeaseWaitCancelled):
        await asyncio.wait_for(task, timeout=5)

    assert pump.lease is None
    assert await holder_still_owns(store, held)


async def test_waiter_task_cancelled_while_waiting_releases_nothing_it_does_not_own(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    held = await hold(store)
    persistence, pump = waiter(store, quick(10))

    task = asyncio.create_task(persistence.run(pump))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pump.lease is None
    assert await holder_still_owns(store, held)


async def test_a_holder_that_dies_lets_the_waiter_in_once_its_ttl_lapses(
    stores: Callable[[], StateStore],
) -> None:
    store = stores()
    await hold(store)
    clock = [NOW]
    persistence, pump = waiter(store, quick(10), clock=lambda: clock[0], key="successor")

    task = asyncio.create_task(persistence.run(pump))
    await asyncio.sleep(0.05)
    assert not task.done(), "the holder's lease is still live"

    clock[0] = NOW + TTL + timedelta(seconds=1)  # the holder never renews or releases
    assert await asyncio.wait_for(task, timeout=5) == "ran"
    assert pump.lease is not None and pump.lease.took_over


async def test_a_waiter_whose_binding_was_replaced_meanwhile_does_not_run_on_it(
    stores: Callable[[], StateStore],
) -> None:
    """The holder replaced the session while the waiter queued: the waiter must
    hand back to its caller (which adopts the replacement), not send on the old one."""
    store = stores()
    held = await hold(store)
    persistence, pump = waiter(store, quick(10))

    task = asyncio.create_task(persistence.run(pump))
    await asyncio.sleep(0.05)
    replacement = BINDING.model_copy(
        update={"generation": 2, "native_refs": {"session": "replacement"}}
    )
    await store.put_binding(replacement, expected_generation=1)
    await store.release_lease(held)

    with pytest.raises(BindingSuperseded):
        await asyncio.wait_for(task, timeout=5)
    assert pump.lease is None
    # The waiter took and released the lease; it is free for the next turn.
    free = await store.acquire_lease(
        binding_slot(BINDING), holder="next", turn_id="next-root", now=NOW, ttl=TTL
    )
    assert free.fence == held.fence + 2


async def test_without_a_wait_a_busy_lease_is_still_refused(
    stores: Callable[[], StateStore],
) -> None:
    from mux.state.lease import LeaseBusy

    store = stores()
    await hold(store)
    persistence = TurnPersistence(store, BINDING, SCOPE, operation_key="plain", now=lambda: NOW)

    with pytest.raises(LeaseBusy):
        await persistence.run(Pumped(persistence))
