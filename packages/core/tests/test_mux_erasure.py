"""Account erasure of the provider-neutral (mux) state, on a real database.

Covers `stores.mux_erasure` through its two callers (`privacy.collect_purge_preview`
and `purge.purge_account`): what is erased, what is kept, that settlement still works,
and that no retained scope, lease or session can write for an erased account.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from daimon.core import usage_outbox_drain
from daimon.core._models import PlatformPrincipal
from daimon.core.accounting_outbox import apply_usage_outbox, record_observation_usage
from daimon.core.pricing import ModelRates, provider_prices
from daimon.core.privacy import PurgePreview, collect_purge_preview, summary_line
from daimon.core.purge import AccountPurgeResult, PurgeReport, purge_account
from daimon.core.shared_threads import shared_owner
from daimon.core.stores import mux_erasure, mux_state, thread_provider_releases
from daimon.core.stores import selfhosted_usage as billing_contexts
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.tenants import delete_tenant, get_tenant, set_provision_status
from daimon.core.usage_outbox_drain import drain_pending_usage
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.journal import JournalEntry
from mux.state.lease import Lease, Slot
from mux.state.memory import MemoryStateStore
from mux.state.usage_ledger import OutboxRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 26, tzinfo=UTC)
TTL = timedelta(minutes=10)
GEMINI = "gemini.inline_reuse"
Factory = async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def sm(db_clean: None, db_committing_engine: AsyncEngine) -> Factory:
    return async_sessionmaker(db_committing_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fresh_drain_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_outbox_drain, "_cursor", None)


@dataclass(frozen=True)
class Person:
    """An account with one platform principal in the world's tenant."""

    account_id: uuid.UUID
    principal_id: uuid.UUID
    tenant_id: uuid.UUID

    @property
    def key(self) -> str:
        return str(self.account_id)

    def scope(self) -> Scope:
        return Scope(
            tenant_id=str(self.tenant_id),
            account_id=self.key,
            principal_id=str(self.principal_id),
            authorization_id="authorized-before-purge",
        )


@dataclass(frozen=True)
class Conversation:
    """One slot with a binding, a held lease, a journal event, usage and an operation."""

    slot: Slot
    binding: ProviderBinding
    lease: Lease
    resource: ResourceRef
    scope: Scope
    operation_key: str


async def make_person(sm: Factory, tenant_id: uuid.UUID, name: str) -> Person:
    """An account and platform principal in `tenant_id`, which is created if it is new."""
    async with sm() as session, session.begin():
        tenant = await get_tenant(session, tenant_id) or await make_tenant(session, id=tenant_id)
        account = await make_account(session, tenant=tenant)
        principal = await make_platform_principal(
            session, platform="discord", external_id=name, tenant=tenant, account=account
        )
    return Person(account_id=account.id, principal_id=principal.id, tenant_id=tenant_id)


async def add_principal(sm: Factory, person: Person, tenant_id: uuid.UUID, name: str) -> None:
    """Give `person`'s account a principal in another tenant (accounts may span tenants)."""
    async with sm() as session, session.begin():
        if await get_tenant(session, tenant_id) is None:
            await make_tenant(session, id=tenant_id)
        session.add(
            PlatformPrincipal(
                tenant_id=tenant_id,
                platform="discord",
                external_id=name,
                account_id=person.account_id,
            )
        )


def message(session_id: str, source: str, words: str) -> JournalEntry:
    return JournalEntry(
        source_key=source,
        event=Event(
            id=f"event-{session_id}-{source}",
            session_id=session_id,
            sequence=0,
            type="agent.message",
            observed_at=NOW,
            authority="record",
            payload={"item_id": source, "content": [{"type": "text", "text": words}]},
            native=NativeProvenance(provider="anthropic", api_revision="synthetic"),
        ),
    )


def usage(resource: ResourceRef, name: str, *, revision: int = 1) -> UsageObservation:
    return UsageObservation(
        id=name,
        revision=revision,
        session=resource,
        model=ModelRef(provider="anthropic", id="synthetic-model"),
        grain="turn",
        basis="cumulative",
        input_tokens=10,
        output_tokens=5,
        completeness="measured",
        observed_at=NOW,
    )


def thread(tenant_id: uuid.UUID, thread_id: str, channel_id: str = "channel") -> ThreadRef:
    channel = ChannelRef(tenant_id=str(tenant_id), platform="discord", channel_id=channel_id)
    return ThreadRef(channel=channel, thread_id=thread_id)


async def converse(
    store: PostgresStateStore,
    person: Person,
    label: str,
    where: ThreadRef,
    *,
    shared: bool = False,
    words: str = "synthetic words",
    release_lease: bool = False,
) -> Conversation:
    """Bind a slot (private to `person`, or shared) and write one of everything to it."""
    slot = Slot(thread=where, account_id=None if shared else person.key)
    binding_id = f"binding-{label}"
    session_id = f"session-{label}"
    binding = ProviderBinding(
        id=binding_id,
        thread=where,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": session_id},
        generation=1,
        config_revision=1,
        legacy_account_id=slot.account_id,
    )
    await store.put_binding(binding, expected_generation=0)
    lease = await store.acquire_lease(
        slot, holder=f"writer-{label}", turn_id=f"turn-{label}", now=NOW, ttl=TTL
    )
    resource = ResourceRef(
        id=session_id,
        kind="session",
        provider="anthropic",
        account_scope_id="synthetic-workspace",
        tenant_id=str(person.tenant_id),
        account_id=person.key,
    )
    await store.append_events(
        resource, [message(session_id, f"{label}-1", words)], fence=lease, cursor="c1", now=NOW
    )
    await store.record_usage(binding_id, usage(resource, f"usage-{label}"))
    scope = person.scope()
    key = f"operation-{label}"
    begun = await store.begin_operation(
        scope, key=key, request_digest="digest", operation_id=f"id-{label}", now=NOW, slot=slot
    )
    assert begun.fresh
    if release_lease:
        await store.release_lease(lease)
    return Conversation(slot, binding, lease, resource, scope, key)


# What the world holds, read straight from the tables.

SURVIVORS: dict[str, str] = {
    "provider_binding_slot": "account_id IS DISTINCT FROM :a",
    "provider_binding": (
        "binding_id IN (SELECT binding_id FROM provider_binding_slot"
        " WHERE account_id IS DISTINCT FROM :a)"
    ),
    "journal_session": (
        "binding_id IN (SELECT binding_id FROM provider_binding_slot"
        " WHERE account_id IS DISTINCT FROM :a)"
    ),
    "journal": (
        "session_id IN (SELECT session_id FROM journal_session WHERE binding_id IN"
        " (SELECT binding_id FROM provider_binding_slot WHERE account_id IS DISTINCT FROM :a))"
    ),
    "thread_lease": "account_id IS DISTINCT FROM :a",
    "thread_provider_release": "account_id IS DISTINCT FROM :a",
    "operation": (
        "account_id <> :a OR (jsonb_typeof(record->'slot') = 'object'"
        " AND record->'slot'->>'account_id' IS NULL)"
    ),
    "usage_observation": "true",
    "accounting_outbox": "true",
}
"""Per table, the rows account `:a`'s erasure must leave byte for byte."""

MUX_TABLES = (*SURVIVORS, "accounting_binding_context", "erased_account")


async def rows(sm: Factory, table: str, where: str = "true", **params: Any) -> list[str]:
    async with sm() as session:
        result = await session.execute(
            text(f"SELECT to_jsonb(t)::text FROM {table} t WHERE {where} ORDER BY 1"), params
        )
        return list(result.scalars())


async def survivors(sm: Factory, person: Person) -> dict[str, list[str]]:
    return {table: await rows(sm, table, where, a=person.key) for table, where in SURVIVORS.items()}


async def count(sm: Factory, table: str, where: str = "true", **params: Any) -> int:
    return len(await rows(sm, table, where, **params))


async def owned_by(sm: Factory, person: Person) -> dict[str, int]:
    """How many rows of each mux table still belong to `person`'s erasable state."""
    key = person.key
    private = "(SELECT binding_id FROM provider_binding_slot WHERE account_id = :a)"
    return {
        "provider_binding_slot": await count(sm, "provider_binding_slot", "account_id = :a", a=key),
        "provider_binding": await count(sm, "provider_binding", f"binding_id IN {private}", a=key),
        "journal_session": await count(sm, "journal_session", f"binding_id IN {private}", a=key),
        "thread_lease": await count(sm, "thread_lease", "account_id = :a", a=key),
        "thread_provider_release": await count(
            sm, "thread_provider_release", "account_id = :a", a=key
        ),
        "operation": await count(
            sm,
            "operation",
            "account_id = :a AND NOT (jsonb_typeof(record->'slot') = 'object'"
            " AND record->'slot'->>'account_id' IS NULL)",
            a=key,
        ),
    }


def counts_of(result: AccountPurgeResult) -> dict[str, int]:
    return {
        name: value for name, value in result.db.model_dump().items() if name.startswith("mux_")
    }


def previewed(preview: PurgePreview) -> dict[str, int]:
    return {
        name: row["count"]
        for name, row in preview.model_dump().items()
        if name.startswith("mux_") and isinstance(row, dict)
    }


@dataclass
class World:
    sm: Factory
    store: PostgresStateStore
    tenant_id: uuid.UUID
    a: Person
    b: Person
    a_private: Conversation
    b_private: Conversation
    shared_from_a: Conversation
    shared_from_b: Conversation


async def build_world(sm: Factory) -> World:
    """A and B in one tenant: private slots in one thread, and one shared thread."""
    tenant_id = uuid.uuid4()
    a = await make_person(sm, tenant_id, "person-a")
    b = await make_person(sm, tenant_id, "person-b")
    store = PostgresStateStore(sm)
    where = thread(tenant_id, "private-thread")
    a_private = await converse(store, a, "private-a", where, words="A private words")
    b_private = await converse(store, b, "private-b", where, words="B private words")
    shared_where = thread(tenant_id, "shared-thread")
    shared_from_a = await converse(
        store, a, "shared", shared_where, shared=True, words="shared words from A"
    )
    # B writes into the same shared session, with the shared slot's lease taken over.
    await store.release_lease(shared_from_a.lease)
    lease = await store.acquire_lease(
        shared_from_a.slot, holder="writer-b", turn_id="turn-b", now=NOW, ttl=TTL
    )
    await store.append_events(
        shared_from_a.resource.model_copy(update={"account_id": b.key}),
        [message(shared_from_a.resource.id, "shared-b", "shared words from B")],
        fence=lease,
        cursor="c2",
        now=NOW,
    )
    begun = await store.begin_operation(
        b.scope(),
        key="operation-shared-b",
        request_digest="digest",
        operation_id="id-shared-b",
        now=NOW,
        slot=shared_from_a.slot,
    )
    assert begun.fresh
    shared_from_b = Conversation(
        shared_from_a.slot,
        shared_from_a.binding,
        lease,
        shared_from_a.resource,
        b.scope(),
        "operation-shared-b",
    )
    await store.release_lease(lease)
    async with sm() as session, session.begin():
        for private in (a_private, b_private):
            await thread_provider_releases.put_request(session, private.slot, generation=1, now=NOW)
    await store.release_lease(a_private.lease)
    await store.release_lease(b_private.lease)
    return World(sm, store, tenant_id, a, b, a_private, b_private, shared_from_a, shared_from_b)


@pytest_asyncio.fixture
async def world(sm: Factory) -> World:
    return await build_world(sm)


async def erase_a(world: World) -> AccountPurgeResult:
    return await purge_account(sm=world.sm, account_id=world.a.account_id)


# Erasure and the preview


async def test_a_private_state_is_erased_and_b_and_shared_state_are_left_byte_for_byte(
    world: World,
) -> None:
    before = await survivors(world.sm, world.a)
    shared_before = await world.store.read_events(world.shared_from_a.resource.id)
    assert {"shared words from A", "shared words from B"} == {
        event.payload["content"][0]["text"]
        for event in shared_before  # type: ignore[index]
    }
    owned = await owned_by(world.sm, world.a)
    assert all(owned.values()), f"the fixture should give A rows in every table: {owned}"

    preview = await collect_purge_preview(sm=world.sm, account_id=world.a.account_id)
    result = await erase_a(world)

    assert await owned_by(world.sm, world.a) == dict.fromkeys(owned, 0)
    assert await survivors(world.sm, world.a) == before
    assert await world.store.read_events(world.shared_from_a.resource.id) == shared_before
    assert await world.store.read_events(world.a_private.resource.id) == []
    assert await world.store.projection(world.a_private.resource.id) is None
    assert await world.store.get_binding(world.a_private.slot) is None
    assert await world.store.read_events(world.b_private.resource.id)
    assert await world.store.get_binding(world.b_private.slot) == world.b_private.binding
    assert counts_of(result) == previewed(preview), "the preview must count what purge removes"
    assert counts_of(result) == {
        "mux_slots": 1,
        "mux_binding_generations": 1,
        "mux_journal_sessions": 1,
        "mux_journal_events": 1,
        "mux_leases": 1,
        "mux_release_requests": 1,
        "mux_operations": 1,
    }


async def test_the_preview_discloses_what_is_retained(world: World) -> None:
    preview = await collect_purge_preview(sm=world.sm, account_id=world.a.account_id)
    assert preview.retained_shared_conversations.count == 1
    assert preview.retained_shared_operations.count == 1
    assert preview.retained_accounting_records.count == 1
    result = await erase_a(world)
    assert result.mux_retained.shared_conversations == 1
    assert result.mux_retained.shared_operations == 1
    assert result.mux_retained.accounting_records == 1
    assert "saved conversation" in summary_line(preview)


async def test_a_second_purge_changes_nothing(world: World) -> None:
    await erase_a(world)
    after_first = {table: await rows(world.sm, table) for table in MUX_TABLES}
    second = await erase_a(world)
    assert set(counts_of(second).values()) == {0}
    assert second.mux_retained.total == second.mux_retained.shared_conversations + (
        second.mux_retained.shared_operations
    )
    assert {table: await rows(world.sm, table) for table in MUX_TABLES} == after_first


async def test_no_private_slot_becomes_shared(world: World) -> None:
    shared_before = await rows(world.sm, "provider_binding_slot", "account_id IS NULL")
    await erase_a(world)
    assert await rows(world.sm, "provider_binding_slot", "account_id IS NULL") == shared_before
    assert await count(world.sm, "thread_lease", "account_id IS NULL") == 1
    # No binding generation was rewritten to name no owner either.
    assert (
        await count(
            world.sm,
            "provider_binding",
            "binding->>'legacy_account_id' IS NULL AND binding_id <> :shared",
            shared=world.shared_from_a.binding.id,
        )
        == 0
    )


async def test_the_shared_slot_stays_usable_for_b_and_not_for_a(world: World) -> None:
    await erase_a(world)
    store = world.store
    lease = await store.acquire_lease(
        world.shared_from_a.slot, holder="b-after", turn_id="t-after", now=NOW, ttl=TTL
    )
    appended = await store.append_events(
        world.shared_from_a.resource.model_copy(update={"account_id": world.b.key}),
        [message(world.shared_from_a.resource.id, "after", "B after A left")],
        fence=lease,
        cursor="c3",
        now=NOW,
    )
    assert len(appended.appended) == 1
    begun = await store.begin_operation(
        world.b.scope(),
        key="after",
        request_digest="d",
        operation_id="id-after",
        now=NOW,
        slot=world.shared_from_a.slot,
    )
    assert begun.fresh
    # A's request against the shared slot was kept, but A can no longer act.
    assert await store.get_operation(world.a.scope(), world.shared_from_a.operation_key)
    with pytest.raises(ScopeViolation):
        await store.begin_operation(
            world.a.scope(),
            key="a-after",
            request_digest="d",
            operation_id="id-a",
            now=NOW,
            slot=world.shared_from_a.slot,
        )
    with pytest.raises(ScopeViolation):
        await store.append_events(
            world.shared_from_a.resource,  # names A as the writer
            [message(world.shared_from_a.resource.id, "a-late", "A late words")],
            fence=lease,
            cursor="c4",
            now=NOW,
        )


async def test_old_generations_are_erased_with_the_slot(world: World) -> None:
    binding = world.a_private.binding
    for generation in (2, 3):
        newer = binding.model_copy(
            update={
                "generation": generation,
                "native_refs": {"session": f"session-private-a-{generation}"},
            }
        )
        await world.store.put_binding(newer, expected_generation=generation - 1)
    assert await count(world.sm, "provider_binding", "binding_id = :id", id=binding.id) == 3
    assert await count(world.sm, "journal_session", "binding_id = :id", id=binding.id) == 3
    result = await erase_a(world)
    assert result.db.mux_binding_generations == 3 and result.db.mux_journal_sessions == 3
    assert await count(world.sm, "provider_binding", "binding_id = :id", id=binding.id) == 0
    assert await count(world.sm, "journal_session", "binding_id = :id", id=binding.id) == 0


async def test_orphaned_rows_are_erased_by_the_account_key_alone(sm: Factory) -> None:
    """Leases, release requests and operations keyed to the account survive no principal link."""
    tenant_id = uuid.uuid4()
    person = await make_person(sm, tenant_id, "orphan")
    store = PostgresStateStore(sm)
    slot = Slot(thread=thread(tenant_id, "orphan-thread"), account_id=person.key)
    await store.acquire_lease(slot, holder="h", turn_id="t", now=NOW, ttl=TTL)
    async with sm() as session, session.begin():
        await thread_provider_releases.put_request(session, slot, generation=0, now=NOW)
    await store.begin_operation(
        person.scope(), key="by-account", request_digest="d", operation_id="o2", now=NOW
    )
    # A synthetic billing operation names neither the account nor a principal.
    async with sm() as session, session.begin():
        await billing_contexts.create_context(
            session,
            tenant_id=tenant_id,
            binding_id="binding-synthetic",
            key="selfhosted-billing:receipt:x",
            value={"admission": {"synthetic": True}},
        )
        # Its principal link is long gone.
        await session.execute(
            text("DELETE FROM platform_principals WHERE id = :p"), {"p": person.principal_id}
        )
    preview = await collect_purge_preview(sm=sm, account_id=person.account_id)
    result = await purge_account(sm=sm, account_id=person.account_id)
    assert previewed(preview) == counts_of(result)
    assert (result.db.mux_leases, result.db.mux_release_requests) == (1, 1)
    assert result.db.mux_operations == 1
    assert await count(sm, "thread_lease", "account_id = :a", a=person.key) == 0
    assert await count(sm, "thread_provider_release", "account_id = :a", a=person.key) == 0
    assert await count(sm, "operation", "key = 'by-account'") == 0
    assert await count(sm, "operation", "account_id LIKE 'selfhosted-billing:%'") == 1


async def test_an_operation_owned_by_a_principal_of_the_account_is_erased(sm: Factory) -> None:
    tenant_id = uuid.uuid4()
    person = await make_person(sm, tenant_id, "by-principal")
    store = PostgresStateStore(sm)
    elsewhere = Scope(
        tenant_id=str(tenant_id),
        account_id="not-the-account-key",
        principal_id=str(person.principal_id),
        authorization_id="z",
    )
    await store.begin_operation(
        elsewhere, key="principal-op", request_digest="d", operation_id="o", now=NOW
    )
    result = await purge_account(sm=sm, account_id=person.account_id)
    assert result.db.mux_operations == 1
    assert await count(sm, "operation", "key = 'principal-op'") == 0


async def test_state_in_every_tenant_goes_including_an_archived_one(sm: Factory) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    person = await make_person(sm, first, "wide")
    other = await make_person(sm, uuid.uuid4(), "elsewhere-other")
    await add_principal(sm, person, second, "wide-2")
    store = PostgresStateStore(sm)
    await converse(store, person, "wide-1", thread(first, "t1"))
    in_second = await converse(
        store,
        Person(person.account_id, person.principal_id, second),
        "wide-2",
        thread(second, "t2"),
    )
    bystander = await converse(
        store, Person(other.account_id, other.principal_id, second), "wide-3", thread(second, "t2")
    )
    await set_provision_status(sm, tenant_id=second, archive=True)
    before_other = {
        t: await rows(sm, t, "account_id = :o", o=other.key) for t in ("thread_lease", "operation")
    }
    result = await purge_account(sm=sm, account_id=person.account_id)
    assert result.db.mux_slots == 2
    assert await count(sm, "provider_binding_slot", "account_id = :a", a=person.key) == 0
    assert {
        t: await rows(sm, t, "account_id = :o", o=other.key) for t in before_other
    } == before_other
    assert await store.get_binding(bystander.slot) == bystander.binding
    # Hard deletion of the archived tenant removes everything left in it, evidence included.
    assert await count(sm, "accounting_outbox", "tenant_id = :t", t=second) == 2
    async with sm() as session, session.begin():
        await delete_tenant(session, tenant_id=second)
    for table in (*MUX_TABLES, "accounting_binding_context"):
        if table != "erased_account":
            assert await count(sm, table, "tenant_id = :t", t=second) == 0, table
    assert in_second.binding.id


async def test_purging_an_account_with_no_mux_state_is_unchanged(sm: Factory) -> None:
    person = await make_person(sm, uuid.uuid4(), "plain")
    preview = await collect_purge_preview(sm=sm, account_id=person.account_id)
    assert set(previewed(preview).values()) == {0}
    assert preview.retained_shared_conversations.count == 0
    result = await purge_account(sm=sm, account_id=person.account_id)
    assert result.db.accounts == 1 and set(counts_of(result).values()) == {0}
    assert result.mux_retained == mux_erasure.MuxRetained()


async def test_the_in_memory_store_has_no_account_fence() -> None:
    """Headless runs keep their store: no database, nothing to erase, nothing fenced."""
    store = MemoryStateStore()
    slot = Slot(thread=thread(uuid.uuid4(), "headless"), account_id=str(uuid.uuid4()))
    lease = await store.acquire_lease(slot, holder="h", turn_id="t", now=NOW, ttl=TTL)
    assert lease.fence == 1


# Settlement evidence


async def pending_observation(
    sm: Factory, binding: ProviderBinding, name: str, *, apply_now: bool
) -> UsageObservation:
    session_ref = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="gemini",
        account_scope_id="owned-workspace",
        tenant_id=binding.thread.channel.tenant_id,
        account_id=binding.legacy_account_id,
    )
    observation = UsageObservation(
        id=name,
        session=session_ref,
        revision=1,
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        turn_id=f"turn-{name}",
        grain="turn",
        basis="cumulative",
        input_tokens=1000,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=100,
        observed_at=datetime.now(UTC),
        completeness="measured",
        native_meter={"offline": True},
    )
    async with sm() as db, db.begin():
        await record_observation_usage(
            db,
            binding_id=binding.id,
            observation=observation,
            tenant_id=uuid.UUID(binding.thread.channel.tenant_id),
            platform_user_id="tester",
            pricing=ModelRates(2, 10, 3, 1),
            provider_price=provider_prices(binding.profile)[("gemini", "gemini-3.8-flash")],
            infrastructure_usd=Decimal(0) if apply_now else None,
            billing_grain="turn",
            channel_id="channel",
        )
    return observation


async def debited(sm: Factory) -> Decimal:
    async with sm() as db:
        return Decimal(
            await db.scalar(
                text("SELECT coalesce(sum(delta_usd), 0) FROM tenant_ledger WHERE delta_usd < 0")
            )
            or 0
        )


async def test_pending_and_applied_usage_settle_exactly_once_after_erasure(sm: Factory) -> None:
    tenant_id = uuid.uuid4()
    person = await make_person(sm, tenant_id, "billing")
    store = PostgresStateStore(sm)
    where = thread(tenant_id, "billing-thread")
    slot = Slot(thread=where, account_id=person.key)
    binding = ProviderBinding(
        id="binding-billing",
        thread=where,
        provider="gemini",
        profile=GEMINI,
        native_refs={"session": "native-billing"},
        generation=1,
        config_revision=0,
        legacy_account_id=person.key,
    )
    await store.put_binding(binding, expected_generation=0)
    assert slot.account_id == binding.legacy_account_id
    # One revision settled while the person existed, one still pending.
    await pending_observation(sm, binding, "settled", apply_now=True)
    await pending_observation(sm, binding, "pending", apply_now=False)
    async with sm() as db, db.begin():
        await billing_contexts.create_context(
            db,
            tenant_id=tenant_id,
            binding_id=binding.id,
            key=billing_contexts.context_key("receipt", binding.id, "pending"),
            value={"admission": {"synthetic": True}},
        )
    paid_before = await debited(sm)
    assert paid_before < 0, "the settled revision debited the tenant"
    outbox_before = await rows(sm, "accounting_outbox")
    observations_before = await rows(sm, "usage_observation")
    assert await count(sm, "accounting_outbox", "applied_at IS NULL") == 1
    assert await count(sm, "accounting_outbox", "applied_at IS NOT NULL") == 1

    result = await purge_account(sm=sm, account_id=person.account_id)
    assert result.db.mux_slots == 1 and result.mux_retained.accounting_records == 2
    assert await count(sm, "provider_binding_slot") == 0
    assert await rows(sm, "accounting_outbox") == outbox_before
    assert await rows(sm, "usage_observation") == observations_before
    assert await count(sm, "accounting_binding_context", "binding_id = :b", b=binding.id) == 1
    context = await rows(sm, "accounting_binding_context")
    assert GEMINI in context[0] and "binding-billing" in context[0]
    assert person.key not in context[0] and "billing-thread" not in context[0]
    # The billing context operation is neither erased nor unreadable.
    async with sm() as db:
        kept = await billing_contexts.read_context(
            db,
            tenant_id=tenant_id,
            binding_id=binding.id,
            key=billing_contexts.context_key("receipt", binding.id, "pending"),
        )
    assert kept == {"admission": {"synthetic": True}}

    # The pending revision settles once from the detached row; the applied one never again.
    applied, usd = await drain_pending_usage(sm, markup=Decimal(1))
    assert applied == 1 and usd > 0
    assert await debited(sm) == paid_before - usd
    assert await count(sm, "accounting_outbox", "applied_at IS NULL") == 0
    assert await drain_pending_usage(sm, markup=Decimal(1)) == (0, Decimal(0))
    assert await debited(sm) == paid_before - usd
    # Replaying a settled row claims nothing and debits nothing.
    async with sm() as db, db.begin():
        body = (await db.execute(text("SELECT row FROM accounting_outbox LIMIT 1"))).scalar_one()
        claimed = await apply_usage_outbox(
            db,
            OutboxRow.model_validate(body),
            tenant_id=tenant_id,
            platform_user_id="tester",
            pricing=ModelRates(2, 10, 3, 1),
            billing_grain="turn",
        )
    assert claimed is False
    assert await debited(sm) == paid_before - usd


async def test_a_tenant_hard_delete_removes_the_settlement_evidence_and_its_context(
    sm: Factory,
) -> None:
    tenant_id = uuid.uuid4()
    person = await make_person(sm, tenant_id, "evidence")
    store = PostgresStateStore(sm)
    where = thread(tenant_id, "evidence-thread")
    convo = await converse(store, person, "evidence", where)
    await purge_account(sm=sm, account_id=person.account_id)
    assert await count(sm, "accounting_outbox", "binding_id = :b", b=convo.binding.id) == 1
    assert await count(sm, "accounting_binding_context") == 1
    async with sm() as session, session.begin():
        await delete_tenant(session, tenant_id=tenant_id)
    assert await count(sm, "accounting_outbox") == 0
    assert await count(sm, "usage_observation") == 0
    assert await count(sm, "accounting_binding_context") == 0


# The fence


async def refused(awaitable: Awaitable[object]) -> None:
    with pytest.raises(ScopeViolation, match="erased"):
        await awaitable


async def test_every_retained_authority_is_refused_after_erasure(world: World) -> None:
    await erase_a(world)
    store, a, retained = world.store, world.a, world.a_private
    before = {table: await rows(world.sm, table) for table in MUX_TABLES}
    await refused(store.put_binding(retained.binding, expected_generation=0))
    await refused(store.acquire_lease(retained.slot, holder="x", turn_id="x", now=NOW, ttl=TTL))
    await refused(store.renew_lease(retained.lease, now=NOW, ttl=TTL))
    await refused(
        store.begin_operation(
            a.scope(),
            key="again",
            request_digest="d",
            operation_id="o",
            now=NOW,
            slot=retained.slot,
        )
    )
    await refused(store.claim_send(retained.scope, retained.operation_key, now=NOW, fence=None))
    await refused(
        store.advance_operation(
            retained.scope, retained.operation_key, "processed", now=NOW, fence=None
        )
    )
    async with world.sm() as session, session.begin():
        await refused(
            thread_provider_releases.put_request(session, retained.slot, generation=1, now=NOW)
        )
    assert {table: await rows(world.sm, table) for table in MUX_TABLES} == before
    assert await store.read_events(retained.resource.id) == []


async def test_a_retained_session_cannot_append_or_record_usage_after_erasure(
    world: World,
) -> None:
    await erase_a(world)
    retained = world.a_private
    # The journal head is gone, so the retained lease has nothing to write to.
    with pytest.raises(ScopeViolation):
        await world.store.append_events(
            retained.resource,
            [message(retained.resource.id, "late", "late words")],
            fence=retained.lease,
            cursor="late",
            now=NOW,
        )
    with pytest.raises(ScopeViolation):
        await world.store.record_usage(retained.binding.id, usage(retained.resource, "late"))
    assert await world.store.read_events(retained.resource.id) == []
    assert await count(world.sm, "usage_observation", "observation_id = 'late'") == 0


async def test_other_accounts_and_synthetic_identities_are_not_fenced(world: World) -> None:
    await erase_a(world)
    store = world.store
    begun = await store.begin_operation(
        world.b.scope(), key="b-new", request_digest="d", operation_id="o", now=NOW
    )
    assert begun.fresh
    synthetic = Scope(
        tenant_id=str(world.tenant_id),
        account_id="selfhosted-billing:binding-x",
        principal_id="host:selfhosted-billing",
        authorization_id="z",
    )
    assert (
        await store.begin_operation(
            synthetic, key="syn", request_digest="d", operation_id="o2", now=NOW
        )
    ).fresh
    # An account id that was never an account is not an erased one.
    stranger = Scope(
        tenant_id=str(world.tenant_id),
        account_id=str(uuid.uuid4()),
        principal_id="p",
        authorization_id="z",
    )
    assert (
        await store.begin_operation(
            stranger, key="stranger", request_digest="d", operation_id="o3", now=NOW
        )
    ).fresh


async def wait_until_blocked(sm: Factory, task: asyncio.Task[Any], person: Person) -> None:
    """Return once a backend waits on `person`'s account lock (other workers' locks don't count)."""
    async with sm() as session:
        for _ in range(100):
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    " AND ((classid::bigint << 32) | objid::bigint) = hashtextextended(:a, 0)"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                ),
                {"a": person.key},
            )
            await session.rollback()
            if waiting:
                return
            assert not task.done(), "the task should be waiting on the account lock"
            await asyncio.sleep(0.05)
    raise AssertionError("nothing waited on the account lock")


async def test_a_writer_that_began_before_erasure_finishes_first_and_is_erased(
    world: World,
) -> None:
    """Order 1: the writer holds the shared account lock, purge waits, then erases its write."""
    async with world.sm() as writer, writer.begin():
        await mux_state.begin_operation(
            writer,
            world.a.scope(),
            key="in-flight",
            request_digest="d",
            operation_id="o",
            now=NOW,
            slot=world.a_private.slot,
        )
        purge = asyncio.create_task(erase_a(world))
        await wait_until_blocked(world.sm, purge, world.a)
        assert not purge.done()
    result = await purge
    assert result.db.mux_operations == 2, "the in-flight operation was erased with the rest"
    assert await count(world.sm, "operation", "key = 'in-flight'") == 0
    await refused(world.store.claim_send(world.a.scope(), "in-flight", now=NOW, fence=None))


async def test_a_writer_that_starts_during_erasure_waits_and_is_then_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order 2: purge holds the exclusive lock; the writer waits, then finds the tombstone."""
    from daimon.core.stores import security_audit

    reached, release = asyncio.Event(), asyncio.Event()
    original = security_audit.erase_account_for_privacy

    async def paused(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(security_audit, "erase_account_for_privacy", paused)
    purge = asyncio.create_task(erase_a(world))
    await asyncio.wait_for(reached.wait(), 10)
    writer = asyncio.create_task(
        world.store.begin_operation(
            world.a.scope(),
            key="racing",
            request_digest="d",
            operation_id="o",
            now=NOW,
            slot=world.a_private.slot,
        )
    )
    await wait_until_blocked(world.sm, writer, world.a)
    assert not writer.done()
    release.set()
    await purge
    with pytest.raises(ScopeViolation, match="erased"):
        await writer
    assert await count(world.sm, "operation", "key = 'racing'") == 0


async def test_erasure_between_begin_and_claim_refuses_the_claim(world: World) -> None:
    """Paused after `begin_operation`, before the send is claimed."""
    scope, slot = world.a.scope(), world.a_private.slot
    await world.store.begin_operation(
        scope, key="paused", request_digest="d", operation_id="o", now=NOW, slot=slot
    )
    lease = await world.store.acquire_lease(slot, holder="h", turn_id="t", now=NOW, ttl=TTL)
    await erase_a(world)
    await refused(world.store.claim_send(scope, "paused", now=NOW, fence=lease))
    assert await count(world.sm, "operation", "key = 'paused'") == 0


async def test_erasure_before_an_append_commits_erases_the_append(world: World) -> None:
    """Paused with the append written but not committed: purge waits and removes it."""
    retained = world.a_private
    lease = await world.store.acquire_lease(
        retained.slot, holder="pause", turn_id="pause", now=NOW, ttl=TTL
    )
    async with world.sm() as writer, writer.begin():
        await mux_state.append_events(
            writer,
            retained.resource,
            [message(retained.resource.id, "paused", "paused words")],
            fence=lease,
            cursor="paused",
            now=NOW,
            clock=mux_state.database_clock,
        )
        purge = asyncio.create_task(erase_a(world))
        await wait_until_blocked(world.sm, purge, world.a)
    result = await purge
    assert result.db.mux_journal_events == 2, "the paused append was erased with the first"
    assert await count(world.sm, "journal", "event::text LIKE '%paused words%'") == 0
    assert await world.store.read_events(retained.resource.id) == []


async def test_erasure_before_a_receipt_commits_erases_it_and_refuses_the_retry(
    world: World,
) -> None:
    """Paused with the receipt (operation result) written but not committed."""
    scope, key = world.a.scope(), world.a_private.operation_key
    lease = await world.store.acquire_lease(
        world.a_private.slot, holder="h", turn_id="t", now=NOW, ttl=TTL
    )
    async with world.sm() as writer, writer.begin():
        await mux_state.claim_send(
            writer, scope, key, now=NOW, clock=mux_state.database_clock, fence=lease
        )
        purge = asyncio.create_task(erase_a(world))
        await wait_until_blocked(world.sm, purge, world.a)
    await purge
    assert await count(world.sm, "operation", "key = :k", k=key) == 0
    await refused(
        world.store.advance_operation(
            scope, key, "processed", now=NOW, fence=lease, result={"x": 1}
        )
    )
    assert await count(world.sm, "operation", "key = :k", k=key) == 0


async def test_reproducer_scenarios_now_end_with_nothing_readable_or_writable(
    world: World,
) -> None:
    """The review's synthetic store scenario: A's lease and scope outlive A's purge."""
    stale_lease, stale_scope = world.a_private.lease, world.a.scope()
    shared_text = {
        event.payload["content"][0]["text"]  # type: ignore[index]
        for event in await world.store.read_events(world.shared_from_a.resource.id)
    }
    await erase_a(world)
    assert await world.store.read_events(world.a_private.resource.id) == []
    assert await world.store.projection(world.a_private.resource.id) is None
    with pytest.raises(ScopeViolation):
        await world.store.append_events(
            world.a_private.resource,
            [message(world.a_private.resource.id, "after-purge", "stale writer content")],
            fence=stale_lease,
            cursor="after",
            now=NOW,
        )
    await refused(
        world.store.begin_operation(
            stale_scope,
            key="after-purge",
            request_digest="d",
            operation_id="post",
            now=NOW,
            slot=world.a_private.slot,
        )
    )
    # Shared journal content, by policy, is exactly what it was.
    assert shared_text == {
        event.payload["content"][0]["text"]  # type: ignore[index]
        for event in await world.store.read_events(world.shared_from_a.resource.id)
    }
    assert shared_owner(world.shared_from_a.slot)


async def test_purge_report_merge_carries_the_mux_counts() -> None:
    report = PurgeReport(mux_slots=1, mux_operations=2).merge(PurgeReport(mux_slots=3))
    assert (report.mux_slots, report.mux_operations) == (4, 2)


async def test_the_fence_ignores_ids_that_are_not_accounts(sm: Factory) -> None:
    async with sm() as session, session.begin():
        await mux_erasure.fence_writers(session, None, "operator", "selfhosted-billing:b")
        await mux_erasure.fence_writers(session, str(uuid.uuid4()))
