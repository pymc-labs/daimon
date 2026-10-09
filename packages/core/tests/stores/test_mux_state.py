"""The Postgres StateStore: the mux protocol suite, plus what only a database can show."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core._models import ProviderBinding as BindingRow
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation
from mux.state.lease import Slot, StaleFence
from mux.state.suite import CHECKS, TENANTS, StoreMaker
from sqlalchemy import select
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
