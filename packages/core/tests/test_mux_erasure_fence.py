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
from daimon.core import purge as purge_module
from daimon.core.purge import AccountPurgeFailed, purge_account
from daimon.core.stores import mux_erasure, mux_state, security_audit, thread_provider_releases
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


@pytest.fixture
def pause_purge_after_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold a purge right after it took the account lock; returns (reached, release)."""
    reached, release = asyncio.Event(), asyncio.Event()
    original = security_audit.erase_account_for_privacy

    async def paused(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(security_audit, "erase_account_for_privacy", paused)
    return reached, release


async def test_a_process_from_before_the_fence_is_refused_during_a_running_purge(
    sm: Factory,
    store_before_the_fence: types.ModuleType,
    pause_purge_after_its_lock: tuple[asyncio.Event, asyncio.Event],
) -> None:
    """The trigger never waits: a write that meets a running purge is refused at once."""
    reached, release = pause_purge_after_its_lock
    person = await make_person(sm, uuid.uuid4(), "old-racing")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    await converse(current, person, "old-racing", thread(person.tenant_id, "t"))
    purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
    try:
        await asyncio.wait_for(reached.wait(), 10)
        old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
        with pytest.raises(DBAPIError, match="the account is being erased"):
            await asyncio.wait_for(
                old.begin_operation(
                    person.scope(), key="racing", request_digest="d", operation_id="o", now=NOW
                ),
                10,
            )
    finally:
        release.set()
        await purge
    assert await count(sm, "operation", "key = 'racing'") == 0


async def test_purge_waits_for_an_old_writer_that_got_in_first_and_then_succeeds(
    sm: Factory, store_before_the_fence: types.ModuleType
) -> None:
    """A writer past its trigger holds the account lock shared; purge waits, then erases it."""
    person = await make_person(sm, uuid.uuid4(), "old-first")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    conversation = await converse(current, person, "old-first", thread(person.tenant_id, "t"))
    purge = None
    try:
        async with sm() as writer, writer.begin():
            await store_before_the_fence.begin_operation(
                writer,
                person.scope(),
                key="old-in-flight",
                request_digest="d",
                operation_id="o",
                now=NOW,
                slot=conversation.slot,
            )
            purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
            await wait_until_blocked(sm, purge, person)
            assert not purge.done()
        result = await asyncio.wait_for(purge, 10)
    finally:
        if purge is not None and not purge.done():
            purge.cancel()
    assert result.db.mux_operations == 2
    assert await count(sm, "operation", "key = 'old-in-flight'") == 0
    assert await count(sm, "accounts", "id = :a", a=person.account_id) == 0


async def _waits_for(sm: Factory, event: str) -> None:
    for _ in range(250):
        async with sm() as db:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()"
                    " AND datname = current_database() AND wait_event = :event"
                ),
                {"event": event},
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"nothing waited on {event}")


@pytest.mark.parametrize("kind", ["current_release", "old_renew", "old_binding", "old_operation"])
async def test_a_writer_holding_a_row_never_makes_a_purge_fail(
    sm: Factory,
    store_before_the_fence: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A writer holds its row, a purge of its account queues behind it, the writer resumes.

    New code took the account fence before the row, so the purge waits for the fence and
    the writer finishes. An old process meets the trigger only after its row lock; the
    trigger refuses instead of waiting for the purge, so no cycle forms and the purge, which
    would otherwise be Postgres's deadlock victim, deletes the account.
    """
    person = await make_person(sm, uuid.uuid4(), f"inversion-{kind}")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    conversation = await converse(store, person, kind, thread(person.tenant_id, "t"))
    row_locked, resume = asyncio.Event(), asyncio.Event()
    module = mux_state if kind == "current_release" else store_before_the_fence
    hook = {
        "current_release": "_save_lease",
        "old_renew": "_save_lease",
        "old_binding": "_binding",
        "old_operation": "_save_operation",
    }[kind]
    original = getattr(module, hook)

    async def paused(*args: Any, **kwargs: Any) -> Any:
        row_locked.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, hook, paused)
    old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
    action = {
        "current_release": lambda: store.release_lease(conversation.lease),
        "old_renew": lambda: old.renew_lease(conversation.lease, now=NOW, ttl=TTL),
        "old_binding": lambda: old.put_binding(
            conversation.binding.model_copy(update={"generation": 2}), expected_generation=1
        ),
        "old_operation": lambda: old.claim_send(
            person.scope(), conversation.operation_key, now=NOW, fence=conversation.lease
        ),
    }[kind]()
    writer = asyncio.create_task(action)
    purge = None
    try:
        await asyncio.wait_for(row_locked.wait(), 10)
        purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
        if kind == "current_release":
            await wait_until_blocked(sm, purge, person)
        else:
            await _waits_for(sm, "transactionid")
        resume.set()
        outcome = await asyncio.wait_for(asyncio.gather(writer, purge, return_exceptions=True), 15)
    finally:
        resume.set()
        for task in (writer, purge):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (writer, purge) if t), return_exceptions=True)
    written, purged = outcome
    assert not isinstance(purged, BaseException), f"the purge failed: {purged!r}"
    assert await count(sm, "accounts", "id = :a", a=person.account_id) == 0
    assert await count(sm, "provider_binding_slot", "account_id = :a", a=person.key) == 0
    if kind == "current_release":
        assert not isinstance(written, BaseException), f"the release failed: {written!r}"
    else:
        assert isinstance(written, DBAPIError) and "being erased" in str(written)


async def test_an_old_thread_release_and_a_purge_both_finish(
    sm: Factory, store_before_the_fence: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old release keeps the request row in one transaction and writes the binding in
    another; Postgres cannot see that dependency, so the second must be refused, not wait."""
    source = subprocess.run(
        ["git", "show", f"{STORE_BEFORE_THE_FENCE}:packages/core/daimon/core/thread_release.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_release = types.ModuleType("thread_release_before_the_fence")
    exec(compile(source, "thread_release_before_the_fence.py", "exec"), old_release.__dict__)
    person = await make_person(sm, uuid.uuid4(), "old-thread-release")
    current = PostgresStateStore(sm, trust_caller_clock=True)
    conversation = await converse(current, person, "old-release", thread(person.tenant_id, "t"))
    async with sm.begin() as session:
        await thread_provider_releases.put_request(
            session, conversation.slot, generation=1, now=NOW
        )
    old = store_before_the_fence.PostgresStateStore(sm, trust_caller_clock=True)
    at_the_binding, resume = asyncio.Event(), asyncio.Event()
    original = old.put_binding

    async def paused(*args: Any, **kwargs: Any) -> Any:
        at_the_binding.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(old, "put_binding", paused)
    release = asyncio.create_task(
        old_release.apply_thread_release(
            sm,
            old,
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
        await _waits_for(sm, "transactionid")
        resume.set()
        outcome = await asyncio.wait_for(asyncio.gather(release, purge, return_exceptions=True), 15)
    finally:
        resume.set()
        for task in (release, purge):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (release, purge) if t), return_exceptions=True)
    released, purged = outcome
    assert not isinstance(purged, BaseException), f"the purge failed: {purged!r}"
    assert isinstance(released, DBAPIError) and "being erased" in str(released)
    assert await count(sm, "accounts", "id = :a", a=person.account_id) == 0


# A purge that cannot get its lock


async def test_a_purge_retries_a_lock_it_could_not_get_and_then_succeeds(
    sm: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(purge_module, "PURGE_LOCK_TIMEOUT", "150ms")
    person = await make_person(sm, uuid.uuid4(), "retry-then-succeed")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    await converse(store, person, "retry", thread(person.tenant_id, "t"))
    purge = None
    try:
        async with sm() as writer, writer.begin():
            await mux_erasure.fence_writers(writer, person.key)
            purge = asyncio.create_task(purge_account(sm=sm, account_id=person.account_id))
            await wait_until_blocked(sm, purge, person)
            await asyncio.sleep(0.5)  # the first attempt times out and starts over
            assert not purge.done()
        result = await asyncio.wait_for(purge, 15)
    finally:
        if purge is not None and not purge.done():
            purge.cancel()
    assert result.db.accounts == 1
    assert await count(sm, "accounts", "id = :a", a=person.account_id) == 0


async def test_a_purge_that_never_gets_its_lock_fails_and_deletes_nothing(
    sm: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(purge_module, "PURGE_LOCK_TIMEOUT", "100ms")
    monkeypatch.setattr(purge_module, "PURGE_ATTEMPTS", 2)
    person = await make_person(sm, uuid.uuid4(), "retry-then-fail")
    store = PostgresStateStore(sm, trust_caller_clock=True)
    await converse(store, person, "retry-fail", thread(person.tenant_id, "t"))
    async with sm() as writer, writer.begin():
        await mux_erasure.fence_writers(writer, person.key)
        with pytest.raises(AccountPurgeFailed, match="nothing was deleted"):
            await asyncio.wait_for(purge_account(sm=sm, account_id=person.account_id), 15)
    assert await count(sm, "accounts", "id = :a", a=person.account_id) == 1
    assert await count(sm, "provider_binding_slot", "account_id = :a", a=person.key) == 1
    assert await count(sm, "erased_account", "account_id = :a", a=person.account_id) == 0


async def test_a_purge_does_not_retry_an_error_that_is_not_a_lock_failure(
    sm: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    person = await make_person(sm, uuid.uuid4(), "no-retry")
    attempts = 0

    async def broken(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        await args[0].execute(text("SELECT 1/0"))

    monkeypatch.setattr(security_audit, "erase_account_for_privacy", broken)
    with pytest.raises(DBAPIError):
        await purge_account(sm=sm, account_id=person.account_id)
    assert attempts == 1


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
