"""The erasure fence holds for every writer: releases, principals, shared usage, old processes.

Regression tests for four ways a write could still reach an erased account's state: a
provider release deadlocking against the purge, an operation owned by an erased principal,
usage recorded for a shared conversation by an erased caller, and a process still running
the store from before the fence existed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from daimon.core import _erasure_guards, thread_release
from daimon.core.purge import purge_account
from daimon.core.stores import mux_state, security_audit, thread_provider_releases
from daimon.core.stores.mux_state import PostgresStateStore
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .test_mux_erasure import (
    NOW,
    TTL,
    Factory,
    Person,
    converse,
    count,
    make_person,
    thread,
    usage,
    wait_until_blocked,
)
from .test_mux_erasure import (
    sm as sm,
)

#: origin/main when the fence was written: the store as an old process still runs it.
STORE_BEFORE_THE_FENCE = "7a4e19149"
REPO = Path(__file__).resolve().parents[3]


def principal_only_scope(person: Person) -> Scope:
    """A scope whose account key is not the account's: only its principal names the person."""
    return Scope(
        tenant_id=str(person.tenant_id),
        account_id="not-the-account-key",
        principal_id=str(person.principal_id),
        authorization_id="z",
    )


# A provider release and a purge of the same account


async def test_a_release_and_a_purge_of_its_account_do_not_deadlock(
    sm: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release holds its request row while it writes the binding; purge deletes that row.

    The release used to write the binding in a second transaction, which waited for the
    account lock purge held while purge waited for the request row. Both now happen in
    one transaction that holds the account lock from the start, so the purge waits for it.
    """
    person = await make_person(sm, uuid.uuid4(), "release-and-purge")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    conversation = await converse(store, person, "release", thread(person.tenant_id, "t"))
    await store.release_lease(conversation.lease)
    async with sm.begin() as session:
        await thread_provider_releases.put_request(
            session, conversation.slot, generation=1, now=NOW
        )

    at_the_binding, resume = asyncio.Event(), asyncio.Event()
    original = mux_state.put_binding

    async def held(*args: Any, **kwargs: Any) -> Any:
        at_the_binding.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(mux_state, "put_binding", held)
    release = asyncio.create_task(
        thread_release.apply_thread_release(
            sm,
            store,
            conversation.slot,
            tenant_id=person.tenant_id,
            account_id=person.account_id,
            now=NOW,
        )
    )
    purge = None
    try:
        await asyncio.wait_for(at_the_binding.wait(), 10)
        purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
        await wait_until_blocked(sm, purge, person)
        resume.set()
        assert await asyncio.wait_for(release, 10) is True
        await asyncio.wait_for(purge, 10)
    finally:
        resume.set()
        for task in (release, purge):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(release, *([purge] if purge else []), return_exceptions=True)
    assert await count(sm, "thread_provider_release", "account_id = :a", a=person.key) == 0
    assert await count(sm, "provider_binding_slot", "account_id = :a", a=person.key) == 0


async def test_a_release_after_the_purge_is_refused(sm: Factory) -> None:
    person = await make_person(sm, uuid.uuid4(), "late-release")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    conversation = await converse(store, person, "late", thread(person.tenant_id, "t"))
    await purge_account(sm=sm, account_id=person.account_id)
    with pytest.raises(ScopeViolation, match="erased"):
        await thread_release.apply_thread_release(
            sm,
            store,
            conversation.slot,
            tenant_id=person.tenant_id,
            account_id=person.account_id,
            now=NOW,
        )


# Operations owned by an erased principal


async def test_an_operation_erased_by_its_principal_cannot_come_back(sm: Factory) -> None:
    person = await make_person(sm, uuid.uuid4(), "principal-fence")
    store = PostgresStateStore(sm)
    scope = principal_only_scope(person)
    args = {"key": "principal-op", "request_digest": "d", "operation_id": "o", "now": NOW}
    await store.begin_operation(scope, **args)
    assert (await purge_account(sm=sm, account_id=person.account_id)).db.mux_operations == 1
    with pytest.raises(ScopeViolation, match="erased"):
        await store.begin_operation(scope, **args)
    assert await count(sm, "operation", "key = 'principal-op'") == 0


async def test_a_principal_that_was_never_erased_is_not_fenced(sm: Factory) -> None:
    person = await make_person(sm, uuid.uuid4(), "alive")
    other = await make_person(sm, person.tenant_id, "other")
    await purge_account(sm=sm, account_id=person.account_id)
    begun = await PostgresStateStore(sm).begin_operation(
        principal_only_scope(other), key="k", request_digest="d", operation_id="o", now=NOW
    )
    assert begun.fresh


# Usage recorded for a shared conversation


async def test_an_erased_caller_cannot_record_usage_in_a_shared_conversation(
    sm: Factory,
) -> None:
    person = await make_person(sm, uuid.uuid4(), "shared-usage")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    shared = await converse(
        store, person, "shared-usage", thread(person.tenant_id, "t"), shared=True
    )
    await purge_account(sm=sm, account_id=person.account_id)
    with pytest.raises(ScopeViolation, match="erased"):
        await store.record_usage(shared.binding.id, usage(shared.resource, "after-erasure"))
    assert await count(sm, "usage_observation", "observation_id = 'after-erasure'") == 0
    assert await count(sm, "accounting_outbox", "observation_id = 'after-erasure'") == 0


# A process that predates the fence


@pytest.fixture
def store_before_the_fence() -> Iterator[types.ModuleType]:
    """`stores.mux_state` as it was before the fence, loaded beside the current one."""
    shown = subprocess.run(
        [
            "git",
            "show",
            f"{STORE_BEFORE_THE_FENCE}:packages/core/daimon/core/stores/mux_state.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        pytest.skip(f"commit {STORE_BEFORE_THE_FENCE} is not in this checkout")
    module = types.ModuleType("store_before_the_fence")
    sys.modules[module.__name__] = module
    try:
        exec(compile(shown.stdout, "mux_state_before_the_fence.py", "exec"), module.__dict__)
        yield module
    finally:
        del sys.modules[module.__name__]


async def test_a_process_from_before_the_fence_cannot_recreate_erased_state(
    sm: Factory, store_before_the_fence: types.ModuleType
) -> None:
    person = await make_person(sm, uuid.uuid4(), "old-writer")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    private = await converse(current, person, "old-private", thread(person.tenant_id, "t"))
    shared = await converse(
        current, person, "old-shared", thread(person.tenant_id, "shared"), shared=True
    )
    await purge_account(sm=sm, account_id=person.account_id)
    tables = ("provider_binding_slot", "thread_lease", "operation", "usage_observation")
    before = {table: await count(sm, table) for table in tables}

    old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
    refusal = pytest.raises(DBAPIError, match="the account was erased")
    with refusal:
        await old.put_binding(private.binding, expected_generation=0)
    with refusal:
        await old.acquire_lease(private.slot, holder="old", turn_id="t", now=NOW, ttl=TTL)
    with refusal:
        await old.begin_operation(
            person.scope(), key="old", request_digest="d", operation_id="o", now=NOW
        )
    with refusal:
        await old.begin_operation(
            principal_only_scope(person), key="old-p", request_digest="d", operation_id="o", now=NOW
        )
    with refusal:
        await old.record_usage(shared.binding.id, usage(shared.resource, "old-late"))
    assert {table: await count(sm, table) for table in tables} == before
    assert await current.read_events(private.resource.id) == []


async def test_a_process_from_before_the_fence_still_serves_everyone_else(
    sm: Factory, store_before_the_fence: types.ModuleType
) -> None:
    """Other accounts, synthetic billing identities and settlement of kept rows are unaffected."""
    person = await make_person(sm, uuid.uuid4(), "erased-one")
    other = await make_person(sm, person.tenant_id, "kept-one")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    await converse(current, person, "erased-one", thread(person.tenant_id, "t1"))
    await purge_account(sm=sm, account_id=person.account_id)

    old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
    survivor = await converse(old, other, "kept-one", thread(person.tenant_id, "t2"))
    assert survivor.lease.fence == 1
    synthetic = Scope(
        tenant_id=str(person.tenant_id),
        account_id="selfhosted-billing:binding-x",
        principal_id="host:selfhosted-billing",
        authorization_id="z",
    )
    assert (
        await old.begin_operation(synthetic, key="s", request_digest="d", operation_id="o", now=NOW)
    ).fresh
    # The erased account's usage rows stay settleable: a pending row applies once.
    pending = [row for row in await old.pending_outbox() if row.binding_id == "binding-erased-one"]
    assert len(pending) == 1
    assert await old.mark_outbox_applied(pending[0]) is True
    assert await old.mark_outbox_applied(pending[0]) is False


async def test_a_process_from_before_the_fence_waits_for_a_running_purge(
    sm: Factory, store_before_the_fence: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger takes the account lock shared, as the fence does: no write slips past the purge."""
    person = await make_person(sm, uuid.uuid4(), "old-racing")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    await converse(current, person, "old-racing", thread(person.tenant_id, "t"))
    reached, release = asyncio.Event(), asyncio.Event()
    original = security_audit.erase_account_for_privacy

    async def paused(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(security_audit, "erase_account_for_privacy", paused)
    purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
    await asyncio.wait_for(reached.wait(), 10)
    old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
    writer = asyncio.create_task(
        old.begin_operation(
            person.scope(), key="racing", request_digest="d", operation_id="o", now=NOW
        )
    )
    await wait_until_blocked(sm, writer, person)
    release.set()
    await purge
    with pytest.raises(DBAPIError, match="the account was erased"):
        await writer
    assert await count(sm, "operation", "key = 'racing'") == 0


async def test_the_migration_carries_the_guards_the_models_install(sm: Factory) -> None:
    import importlib.util

    path = Path(__file__).parents[1] / "alembic/versions/0142_mux_account_erasure.py"
    spec = importlib.util.spec_from_file_location("mux_account_erasure_guards", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.GUARD_STATEMENTS == _erasure_guards.INSTALL
    assert migration.GUARD_REMOVAL == _erasure_guards.REMOVE
    async with sm() as session:
        triggers = set(
            await session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid IN (SELECT oid FROM pg_class"
                    " WHERE relnamespace = current_schema()::regnamespace) AND NOT tgisinternal"
                )
            )
        )
    assert {
        _erasure_guards.trigger_name(table) for table in _erasure_guards.GUARDED_TABLES
    } <= triggers
