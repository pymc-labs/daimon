"""The Postgres StateStore: the mux protocol suite, plus what only a database can show."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core._models import ProviderBinding as BindingRow
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.testing.factories import make_tenant
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.journal import JournalEntry
from mux.state.lease import Lease, Slot, StaleFence
from mux.state.operations import recovery
from mux.state.suite import CHECKS, TENANTS, StoreMaker
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@pytest_asyncio.fixture
async def factory(
    db_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Its own pooled connections, so concurrent checks really race."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        for index, tenant_id in enumerate(TENANTS):
            await make_tenant(session, id=uuid.UUID(tenant_id), workspace_id=f"mux-{index}")
    yield sessions


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_postgres_store(
    check: Callable[[StoreMaker], Awaitable[None]],
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await check(lambda: PostgresStateStore(factory, trust_caller_clock=True))


def _slot(tenant_id: str = TENANTS[0]) -> Slot:
    channel = ChannelRef(tenant_id=tenant_id, platform="slack", channel_id="c")
    return Slot(thread=ThreadRef(channel=channel, thread_id="th"), account_id="a1")


async def test_lease_expiry_follows_the_database_clock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PostgresStateStore(factory)
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    lease = await store.acquire_lease(
        _slot(), holder="w1", turn_id="t", now=long_ago, ttl=timedelta(seconds=1)
    )
    # Acquired on the database clock, not the stale one passed in.
    assert lease.acquired_at > datetime(2020, 1, 1, tzinfo=UTC)
    expired = await store.acquire_lease(
        _slot(), holder="w1", turn_id="t", now=long_ago, ttl=timedelta(seconds=0)
    )
    assert expired == lease
    short = await PostgresStateStore(factory).acquire_lease(
        _slot(TENANTS[1]), holder="w", turn_id="t", now=NOW, ttl=timedelta(0)
    )
    # A caller claiming an old `now` cannot keep a lease the database sees as expired.
    with pytest.raises(StaleFence):
        await store.renew_lease(short, now=long_ago, ttl=timedelta(minutes=5))


async def test_a_non_uuid_tenant_is_refused(factory: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ScopeViolation):
        await PostgresStateStore(factory).get_binding(_slot("t1"))


async def test_binding_generations_are_kept_as_history(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PostgresStateStore(factory, trust_caller_clock=True)
    first = ProviderBinding(
        id="b1",
        thread=_slot().thread,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": "s1"},
        generation=1,
        config_revision=1,
        legacy_account_id="a1",
    )
    await store.put_binding(first, expected_generation=0)
    second = first.model_copy(update={"generation": 2, "native_refs": {"session": "s2"}})
    await store.put_binding(second, expected_generation=1)
    async with factory() as session:
        generations = await session.scalars(
            select(BindingRow.generation).where(BindingRow.binding_id == "b1")
        )
        assert sorted(generations) == [1, 2]


# Real-clock contention: the lease decision uses the time after the lock wait.

SCOPE = Scope(tenant_id=TENANTS[0], account_id="a1", principal_id="u1", authorization_id="z")
SESSION = ResourceRef(id="s1", kind="session", provider="anthropic", account_scope_id="ws")
TTL = timedelta(milliseconds=400)
HOLD = 0.9


def _owner_binding() -> ProviderBinding:
    return ProviderBinding(
        id="b1",
        thread=_slot().thread,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": SESSION.id},
        generation=1,
        config_revision=1,
        legacy_account_id="a1",
    )


def _entry(source: str) -> JournalEntry:
    event = Event(
        id=f"ev_{source}",
        session_id=SESSION.id,
        sequence=0,
        type="agent.message",
        observed_at=NOW,
        authority="record",
        payload={"item_id": source, "content": []},
        native=NativeProvenance(provider="anthropic", api_revision="x"),
    )
    return JournalEntry(source_key=source, event=event)


async def _hold(factory: async_sessionmaker[AsyncSession], sql: str, locked: asyncio.Event) -> None:
    async with factory() as session, session.begin():
        await session.execute(text(sql))
        locked.set()
        await asyncio.sleep(HOLD)


async def _behind(
    factory: async_sessionmaker[AsyncSession], sql: str, write: Awaitable[object]
) -> object:
    locked = asyncio.Event()
    holder = asyncio.create_task(_hold(factory, sql, locked))
    await locked.wait()
    try:
        return await write
    finally:
        await holder


async def _setup(factory: async_sessionmaker[AsyncSession]) -> tuple[PostgresStateStore, Lease]:
    store = PostgresStateStore(factory)
    await store.put_binding(_owner_binding(), expected_generation=0)
    await store.begin_operation(
        SCOPE, key="k", request_digest="d", operation_id="o", now=NOW, slot=_slot()
    )
    return store, await store.acquire_lease(_slot(), holder="w1", turn_id="t", now=NOW, ttl=TTL)


async def test_a_claim_that_waited_past_expiry_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    claim = store.claim_send(SCOPE, "k", now=NOW, fence=lease)
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM operation FOR UPDATE", claim)
    record = await store.get_operation(SCOPE, "k")
    assert record is not None and record.operation.status == "pending"


async def test_an_advance_that_waited_past_expiry_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    await store.claim_send(SCOPE, "k", now=NOW, fence=lease)
    advance = store.advance_operation(SCOPE, "k", "accepted", now=NOW, fence=lease)
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM operation FOR UPDATE", advance)


async def test_an_append_that_waited_past_expiry_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    append = store.append_events(SESSION, [_entry("e1")], fence=lease, cursor="c", now=NOW)
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM journal_session FOR UPDATE", append)
    assert await store.read_events(SESSION.id) == []


async def test_fenced_writes_that_waited_on_the_lease_row_past_expiry_are_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    claim = store.claim_send(SCOPE, "k", now=NOW, fence=lease)
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM thread_lease FOR UPDATE", claim)
    append = store.append_events(SESSION, [_entry("e1")], fence=lease, cursor="c", now=NOW)
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM thread_lease FOR UPDATE", append)
    assert await store.read_events(SESSION.id) == []


async def test_a_renewal_that_waited_past_expiry_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    renew = store.renew_lease(lease, now=NOW, ttl=timedelta(minutes=5))
    with pytest.raises(StaleFence):
        await _behind(factory, "SELECT 1 FROM thread_lease FOR UPDATE", renew)


async def test_an_acquire_that_waited_past_expiry_takes_over(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store, lease = await _setup(factory)
    acquire = store.acquire_lease(_slot(), holder="w2", turn_id="t2", now=NOW, ttl=TTL)
    successor = await _behind(factory, "SELECT 1 FROM thread_lease FOR UPDATE", acquire)
    assert isinstance(successor, Lease)
    assert successor.took_over and successor.fence == lease.fence + 1


# Transaction boundaries: a rolled-back step leaves nothing; a committed one survives a restart.


class _Crash(Exception):
    pass


async def _crashing(
    factory: async_sessionmaker[AsyncSession],
    step: Callable[[AsyncSession], Awaitable[object]],
) -> None:
    with pytest.raises(_Crash):
        async with factory() as session, session.begin():
            await step(session)
            raise _Crash


async def test_send_claim_and_acceptance_boundaries(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PostgresStateStore(factory, trust_caller_clock=True)
    await store.put_binding(_owner_binding(), expected_generation=0)
    lease = await store.acquire_lease(_slot(), holder="w", turn_id="t", now=NOW, ttl=TTL)
    await store.begin_operation(
        SCOPE, key="k", request_digest="d", operation_id="o", now=NOW, slot=_slot()
    )
    clock = mux_state.fixed_clock(NOW)

    await _crashing(
        factory,
        lambda s: mux_state.claim_send(s, SCOPE, "k", now=NOW, clock=clock, fence=lease),
    )
    record = await PostgresStateStore(factory).get_operation(SCOPE, "k")
    assert record is not None and recovery(record.operation) == "send"

    await store.claim_send(SCOPE, "k", now=NOW, fence=lease)
    await _crashing(
        factory,
        lambda s: mux_state.advance_operation(
            s, SCOPE, "k", "accepted", now=NOW, clock=clock, fence=lease
        ),
    )
    record = await PostgresStateStore(factory).get_operation(SCOPE, "k")
    assert record is not None and recovery(record.operation) == "reconcile"

    await store.advance_operation(SCOPE, "k", "accepted", now=NOW, fence=lease)
    record = await PostgresStateStore(factory).get_operation(SCOPE, "k")
    assert record is not None and recovery(record.operation) == "observe"


async def test_journal_boundaries(factory: async_sessionmaker[AsyncSession]) -> None:
    store = PostgresStateStore(factory, trust_caller_clock=True)
    await store.put_binding(_owner_binding(), expected_generation=0)
    lease = await store.acquire_lease(_slot(), holder="w", turn_id="t", now=NOW, ttl=TTL)
    clock = mux_state.fixed_clock(NOW)
    entries = [_entry("e1"), _entry("e2")]

    await _crashing(
        factory,
        lambda s: mux_state.append_events(
            s, SESSION, entries, fence=lease, cursor="c2", now=NOW, clock=clock
        ),
    )
    restarted = PostgresStateStore(factory, trust_caller_clock=True)
    assert await restarted.read_events(SESSION.id) == []
    assert await restarted.projection(SESSION.id) is None

    await store.append_events(SESSION, entries, fence=lease, cursor="c2", now=NOW)
    replay = await restarted.append_events(
        SESSION, [*entries, _entry("e3")], fence=lease, cursor="c3", now=NOW
    )
    assert replay.duplicates == 2
    assert [e.sequence for e in await restarted.read_events(SESSION.id)] == [0, 1, 2]


async def test_usage_and_outbox_boundaries(factory: async_sessionmaker[AsyncSession]) -> None:
    store = PostgresStateStore(factory, trust_caller_clock=True)
    await store.put_binding(_owner_binding(), expected_generation=0)
    observation = UsageObservation(
        id="obs",
        revision=1,
        session=SESSION,
        grain="model_request",
        basis="cumulative",
        output_tokens=100,
        completeness="measured",
        observed_at=NOW,
    )

    await _crashing(factory, lambda s: mux_state.record_usage(s, "b1", observation))
    assert await store.pending_outbox() == []

    row = await store.record_usage("b1", observation)
    assert row is not None
    # The host's ledger transaction failed: the row stays pending, to apply again.
    await _crashing(factory, lambda s: mux_state.mark_outbox_applied(s, row))
    assert [r.key for r in await store.pending_outbox()] == [row.key]
    assert await store.mark_outbox_applied(row)
    assert await PostgresStateStore(factory).pending_outbox() == []
