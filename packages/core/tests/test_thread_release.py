"""Applying a confirmed thread release: once, under real concurrent connections."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from daimon.testing.db import db_nullpool_engine  # noqa: F401


async def test_two_turns_applying_the_same_release_release_the_slot_once(
    db_preflight,
    db_nullpool_engine,  # noqa: F811
) -> None:
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.thread_release import (
        apply_thread_release,
        caller_slot,
        pending_releases,
        request_thread_release,
    )
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_account, make_tenant
    from mux.contracts.config import DEFAULT_BACKEND
    from mux.contracts.resources import ProviderBinding
    from mux.profiles import MANAGED_AGENTS
    from mux.state.store import is_released
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("Concurrent release proofs require an owned DB or isolated CI")
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    slot = caller_slot(
        tenant_id=tenant.id,
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        account_id=account.id,
    )
    store = PostgresStateStore(sessions)
    bound = await store.put_binding(
        ProviderBinding(
            id=f"binding-{tenant.id}",
            thread=slot.thread,
            provider=DEFAULT_BACKEND,
            profile=MANAGED_AGENTS.profile_id,
            native_refs={"session": f"sess-{tenant.id}"},
            generation=1,
            config_revision=0,
            legacy_account_id=slot.account_id,
        ),
        expected_generation=0,
    )
    async with sessions.begin() as session:
        await request_thread_release(session, slot, generation=1, now=datetime.now(UTC))

    def apply():
        return apply_thread_release(
            sessions,
            PostgresStateStore(sessions),
            slot,
            tenant_id=tenant.id,
            account_id=account.id,
            now=datetime.now(UTC),
        )

    results = await asyncio.gather(apply(), apply())
    assert sorted(results) == [False, True]
    released = await store.get_binding(slot, include_released=True)
    assert is_released(released) and released.generation == bound.generation + 1
    async with sessions() as session:
        assert await pending_releases(session, tenant_id=tenant.id) == []
