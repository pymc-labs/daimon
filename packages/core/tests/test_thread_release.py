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


async def test_a_released_openai_session_loses_its_keys_file(
    db_session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing mounts a released session's keys again, so its file is deleted at the provider."""
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.thread_release import caller_slot, release_now
    from daimon.core.turn import openai_deployment
    from daimon.testing.factories import make_account, make_tenant
    from mux.contracts.resources import ProviderBinding

    calls: list[tuple[str, str]] = []

    class _Upstream:
        async def request(self, method: str, path: str, **_: object) -> dict[str, object]:
            calls.append((method, path))
            if path.startswith("/files/") and method == "GET":
                return {"id": "f-1", "filename": ".env", "bytes": 9, "created_at": 0}
            return {}

    monkeypatch.setattr(openai_deployment, "usage_transport", lambda *_: _Upstream())
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    slot = caller_slot(
        tenant_id=tenant.id,
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        account_id=account.id,
    )
    store = PostgresStateStore(db_session_factory)
    await store.put_binding(
        ProviderBinding(
            id=f"binding-{tenant.id}",
            thread=slot.thread,
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": "s-1", "account_scope": "proj", "env_file": "file:f-1"},
            generation=1,
            config_revision=0,
            legacy_account_id=slot.account_id,
        ),
        expected_generation=0,
    )
    released = await release_now(
        db_session_factory,
        store,
        slot,
        generation=1,
        tenant_id=tenant.id,
        account_id=account.id,
        now=datetime.now(UTC),
    )
    assert released, "the slot is released"
    assert ("DELETE", "/files/f-1") in calls, "the released session's keys file is deleted"
