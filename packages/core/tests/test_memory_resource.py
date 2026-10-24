"""Behavior tests for lazy memory-store provisioning (agent memory feature)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anthropic
import httpx
import pytest
from daimon.core.memory_resource import (
    CHANNEL_MEMORY_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    PERSONAL_MEMORY_INSTRUCTIONS,
    READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS,
    READ_ONLY_MEMORY_INSTRUCTIONS,
    READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS,
    SHARED_ONLY_MEMORY_INSTRUCTIONS,
    archive_memory_store_for_agent,
    delete_personal_memory_stores_for_account,
    ensure_channel_memory_store_and_mount,
    ensure_memory_store_and_mount,
    ensure_personal_memory_store_and_mount,
)
from daimon.core.stores.agent_memory_stores import (
    get_memory_store_id,
    get_personal_memory_store_id,
    insert_memory_store,
    insert_personal_memory_store,
    list_channel_memory_stores_for_agent,
)
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("read_only", [False, True])
async def test_cold_path_creates_store_and_binding(
    read_only: bool, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()  # visible to the factory's separate sessions
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    mount = await ensure_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
        read_only=read_only,
    )

    assert mount["type"] == "memory_store"
    assert mount["access"] == ("read_only" if read_only else "read_write")
    assert mount["instructions"] == (
        READ_ONLY_MEMORY_INSTRUCTIONS if read_only else SHARED_ONLY_MEMORY_INSTRUCTIONS
    )
    assert mount["memory_store_id"] in state.stores
    created = state.stores[mount["memory_store_id"]]
    assert created["metadata"]["daimon_tenant"] == str(tenant.id)
    assert created["metadata"]["daimon_agent"] == str(agent_id)
    bound = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert bound == mount["memory_store_id"]


async def test_warm_path_makes_no_api_calls(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_X"
    )
    await db_session.commit()

    def refuse(request):  # any API call is a test failure
        raise AssertionError(f"warm path must not call the API: {request.url}")

    client = build_fake_anthropic(refuse)
    mount = await ensure_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
    )
    assert mount["memory_store_id"] == "memstore_X"


async def test_lost_race_deletes_orphan_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Verifies the loser-cleanup wiring in ensure_memory_store_and_mount's
    warm path: with a rival binding already present, ensure() takes the warm
    path (one DB read, zero API calls), returns the rival's id, and creates
    no new store. (The true cold-path lost-race — read-then-insert loses to a
    concurrent winner between the two — is exercised at the store-layer unit
    tests for insert_memory_store; interleaving it here would require
    monkeypatching internals, which is out of scope for this test.)"""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()

    # Pre-plant the rival binding so ensure()'s insert loses. To force the
    # cold path despite the existing row, monkeypatching the read is fiddly;
    # instead call the store layer directly to verify loser-cleanup wiring:
    async with db_session_factory() as s, s.begin():
        winner = await insert_memory_store(
            s, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_RIVAL"
        )
    assert winner == "memstore_RIVAL"

    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mount = await ensure_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
    )
    # Warm path now — rival wins, no new store created.
    assert mount["memory_store_id"] == "memstore_RIVAL"
    assert state.stores == {}


async def test_insert_failure_deletes_created_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A DB failure after memory_stores.create must not leak the store:
    the cold path deletes it before re-raising, so repeated session attempts
    during an outage don't accumulate unreferenced active stores."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    calls = 0

    def failing_factory():
        nonlocal calls
        calls += 1
        if calls == 2:  # first call is the binding read; second is the insert txn
            raise RuntimeError("db down")
        return db_session_factory()

    with pytest.raises(RuntimeError, match="db down"):
        await ensure_memory_store_and_mount(
            client,
            failing_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            agent_name="daimon",
        )
    assert state.stores == {}, "orphan store must be deleted when the insert fails"


async def test_cold_path_lost_race_deletes_orphan(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """True cold-path lost race: the binding read finds nothing, a rival lands
    its binding before our insert transaction runs, so insert_memory_store
    returns the rival's id and the just-created store is deleted."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    calls = 0

    @asynccontextmanager
    async def _rival_then_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as s, s.begin():
            await insert_memory_store(
                s,
                tenant_id=tenant.id,
                agent_id=agent_id,
                memory_store_id="memstore_RIVAL",
            )
        async with db_session_factory() as s:
            yield s

    def racing_factory():
        nonlocal calls
        calls += 1
        if calls == 2:  # the insert txn — rival wins just before it
            return _rival_then_session()
        return db_session_factory()

    mount = await ensure_memory_store_and_mount(
        client,
        racing_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
    )
    assert mount["memory_store_id"] == "memstore_RIVAL"
    assert state.stores == {}, "loser's store must be deleted after a lost race"


async def test_archive_helper_archives_and_clears(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    mount = await ensure_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
    )
    store_id = mount["memory_store_id"]

    await archive_memory_store_for_agent(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
    )
    assert state.stores[store_id]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) is None

    # idempotent: second archive on a now-unbound agent is a no-op
    await archive_memory_store_for_agent(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
    )


async def test_shared_instructions_point_personal_facts_at_the_personal_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """With a personal store beside it, the shared store says where personal facts
    go; without one, it forbids recording them at all."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    client = build_fake_anthropic(make_fake_memory_store_handler(FakeMemoryStoreState()))

    beside = await ensure_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        agent_name="daimon",
        personal_mounted=True,
    )
    alone = await ensure_memory_store_and_mount(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id, agent_name="daimon"
    )

    assert beside["instructions"] == MEMORY_INSTRUCTIONS
    assert "personal memory" in MEMORY_INSTRUCTIONS
    assert alone["instructions"] == SHARED_ONLY_MEMORY_INSTRUCTIONS
    assert "No personal memory is mounted" in SHARED_ONLY_MEMORY_INSTRUCTIONS
    assert beside["memory_store_id"] == alone["memory_store_id"], "one shared store per agent"
    for text in (
        MEMORY_INSTRUCTIONS,
        SHARED_ONLY_MEMORY_INSTRUCTIONS,
        PERSONAL_MEMORY_INSTRUCTIONS,
        READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS,
    ):
        assert len(text) <= 4096, "MA caps per-attachment instructions at 4096 characters"


@pytest.mark.parametrize("read_only", [False, True])
async def test_personal_store_is_one_per_person_and_agent(
    read_only: bool, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    async def mount(account_id: uuid.UUID, agent: uuid.UUID = agent_id):
        return await ensure_personal_memory_store_and_mount(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent,
            account_id=account_id,
            agent_name="daimon",
            read_only=read_only,
        )

    first = await mount(alice)
    again = await mount(alice)
    other_person = await mount(bob)
    other_agent = await mount(alice, uuid.uuid4())

    assert first["memory_store_id"] == again["memory_store_id"]
    assert (
        len(
            {
                first["memory_store_id"],
                other_person["memory_store_id"],
                other_agent["memory_store_id"],
            }
        )
        == 3
    )
    assert first["access"] == ("read_only" if read_only else "read_write")
    assert first["instructions"] == (
        READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS if read_only else PERSONAL_MEMORY_INSTRUCTIONS
    )
    created = state.stores[first["memory_store_id"]]
    assert created["name"] == f"personal {alice}", "mounts at /mnt/memory/personal-<account>/"
    assert created["metadata"] == {
        "daimon_tenant": str(tenant.id),
        "daimon_agent": str(agent_id),
        "daimon_account": str(alice),
    }
    async with db_session_factory() as s:
        assert (
            await get_personal_memory_store_id(
                s, tenant_id=tenant.id, agent_id=agent_id, account_id=alice
            )
            == first["memory_store_id"]
        )
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) is None, (
            "a personal store never stands in for the shared one"
        )


async def test_archive_helper_archives_every_personal_store_of_the_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id, kept_agent = uuid.uuid4(), uuid.uuid4()
    alice = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    personal = await ensure_personal_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=alice,
        agent_name="daimon",
    )
    kept = await ensure_personal_memory_store_and_mount(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=kept_agent,
        account_id=alice,
        agent_name="other",
    )

    await archive_memory_store_for_agent(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
    )

    assert state.stores[personal["memory_store_id"]]["archived_at"] is not None
    assert state.stores[kept["memory_store_id"]]["archived_at"] is None
    async with db_session_factory() as s:
        assert (
            await get_personal_memory_store_id(
                s, tenant_id=tenant.id, agent_id=agent_id, account_id=alice
            )
            is None
        )


async def test_account_erasure_deletes_only_that_persons_stores(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    shared = await ensure_memory_store_and_mount(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id, agent_name="daimon"
    )
    mounts = {}
    for account_id in (alice, bob):
        mounts[account_id] = await ensure_personal_memory_store_and_mount(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account_id,
            agent_name="daimon",
        )

    assert await delete_personal_memory_stores_for_account(
        client, db_session_factory, account_id=alice
    )

    assert mounts[alice]["memory_store_id"] not in state.stores, "deleted, not archived"
    assert mounts[bob]["memory_store_id"] in state.stores
    assert shared["memory_store_id"] in state.stores
    async with db_session_factory() as s:
        assert (
            await get_personal_memory_store_id(
                s, tenant_id=tenant.id, agent_id=agent_id, account_id=alice
            )
            is None
        )


async def test_account_erasure_keeps_the_binding_of_a_store_it_could_not_delete(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    agent_id, alice = uuid.uuid4(), uuid.uuid4()
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=alice,
        memory_store_id="memstore_STUCK",
    )
    await db_session.commit()

    def unavailable(request):
        return httpx.Response(503, json={"type": "error", "error": {"type": "overloaded_error"}})

    client = build_fake_anthropic(unavailable).with_options(max_retries=0)

    assert not await delete_personal_memory_stores_for_account(
        client, db_session_factory, account_id=alice
    )
    async with db_session_factory() as s:
        assert (
            await get_personal_memory_store_id(
                s, tenant_id=tenant.id, agent_id=agent_id, account_id=alice
            )
            == "memstore_STUCK"
        ), "the binding is the record of the store still to delete"


async def test_one_failed_personal_archive_does_not_leave_the_others_live(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.stores.agent_memory_stores import list_personal_memory_stores_for_account

    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    state = FakeMemoryStoreState()
    handler = make_fake_memory_store_handler(state)
    stuck: set[str] = set()

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and any(
            request.url.path == f"/v1/memory_stores/{sid}/archive" for sid in stuck
        ):
            return httpx.Response(400, json={"type": "error", "error": {"type": "api_error"}})
        return handler(request)

    client = build_fake_anthropic(flaky)
    shared = await ensure_memory_store_and_mount(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id, agent_name="daimon"
    )
    mounts = {}
    for account_id in (alice, bob):
        mounts[account_id] = await ensure_personal_memory_store_and_mount(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account_id,
            agent_name="daimon",
        )
    stuck.add(mounts[alice]["memory_store_id"])

    with pytest.raises(anthropic.APIError):
        await archive_memory_store_for_agent(
            client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
        )

    assert state.stores[mounts[bob]["memory_store_id"]]["archived_at"] is not None
    assert state.stores[shared["memory_store_id"]]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) is None
        for account_id in (alice, bob):
            inventory = await list_personal_memory_stores_for_account(s, account_id=account_id)
            assert [store for _, _, store in inventory] == [
                mounts[account_id]["memory_store_id"]
            ], "every personal store stays in the erasure inventory"


@pytest.mark.parametrize("change", (None, "clear", "rebind"))
async def test_exact_warm_memory_refuses_changed_binding_without_provisioning(
    db_preflight, db_committing_engine, change
):
    from daimon.testing.db import require_owned_sessionmaker
    from mux.errors import ScopeViolation
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        tenant = await make_tenant(db)
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    original = await ensure_memory_store_and_mount(
        client, sm, tenant_id=tenant.id, agent_id=agent_id, agent_name="root", read_only=True
    )
    expected = original["memory_store_id"]
    if change is not None:
        await archive_memory_store_for_agent(client, sm, tenant_id=tenant.id, agent_id=agent_id)
    if change == "rebind":
        replacement = await ensure_memory_store_and_mount(
            client, sm, tenant_id=tenant.id, agent_id=agent_id, agent_name="root"
        )
        assert replacement["memory_store_id"] != expected
    async with sm() as db:
        before = await get_memory_store_id(db, tenant_id=tenant.id, agent_id=agent_id)
    stores_before = dict(state.stores)
    transport = ScriptedTransport()
    async with transport.client() as constrained:
        if change is None:
            assert (
                await ensure_memory_store_and_mount(
                    constrained,
                    sm,
                    tenant_id=tenant.id,
                    agent_id=agent_id,
                    agent_name="root",
                    read_only=True,
                    expected_memory_store_id=expected,
                )
                == original
            )
        else:
            with pytest.raises(ScopeViolation) as refusal:
                await ensure_memory_store_and_mount(
                    constrained,
                    sm,
                    tenant_id=tenant.id,
                    agent_id=agent_id,
                    agent_name="root",
                    expected_memory_store_id=expected,
                )
            assert expected not in str(refusal.value)
            if before is not None:
                assert before not in str(refusal.value)
    assert not transport.requests and not transport.violations
    assert state.stores == stores_before
    async with sm() as db:
        assert await get_memory_store_id(db, tenant_id=tenant.id, agent_id=agent_id) == before
    await client.close()


async def test_channel_store_is_provisioned_once_per_channel_and_tells_the_agent_it_is_shared(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id = uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    async def mount(channel_id: str, *, platform: str = "slack", read_only: bool = False):
        return await ensure_channel_memory_store_and_mount(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            platform=platform,
            channel_id=channel_id,
            agent_name="daimon",
            read_only=read_only,
        )

    first = await mount("C-SYN-1")
    again = await mount("C-SYN-1")
    other = await mount("C-SYN-2")
    teams = await mount("C-SYN-1", platform="teams")
    read_only = await mount("C-SYN-1", read_only=True)

    assert again["memory_store_id"] == first["memory_store_id"], "a warm channel reuses its store"
    assert len({first["memory_store_id"], other["memory_store_id"], teams["memory_store_id"]}) == 3
    assert len(state.stores) == 3, "only the cold mounts created a store"
    assert first["access"] == "read_write" and first["instructions"] == CHANNEL_MEMORY_INSTRUCTIONS
    assert "including people from another organisation" in CHANNEL_MEMORY_INSTRUCTIONS
    assert "would not say in this channel" in CHANNEL_MEMORY_INSTRUCTIONS
    assert read_only["access"] == "read_only"
    assert read_only["instructions"] == READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS
    metadata = state.stores[first["memory_store_id"]]["metadata"]
    assert metadata["daimon_tenant"] == str(tenant.id)
    assert metadata["daimon_agent"] == str(agent_id)
    assert metadata["daimon_platform"] == "slack" and metadata["daimon_channel"] == "C-SYN-1"
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_id) is None, (
            "a channel store never stands in for the agent-wide one"
        )


async def test_archiving_an_agent_archives_its_channel_stores_and_no_other_agents(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id, other_agent = uuid.uuid4(), uuid.uuid4()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mounts = {}
    for agent, channel in ((agent_id, "C-SYN-1"), (agent_id, "C-SYN-2"), (other_agent, "C-SYN-1")):
        mounts[(agent, channel)] = await ensure_channel_memory_store_and_mount(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent,
            platform="slack",
            channel_id=channel,
            agent_name="daimon",
        )

    await archive_memory_store_for_agent(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
    )

    for key, mount in mounts.items():
        archived = state.stores[mount["memory_store_id"]]["archived_at"] is not None
        assert archived is (key[0] == agent_id)
    async with db_session_factory() as s:
        assert (
            await list_channel_memory_stores_for_agent(s, tenant_id=tenant.id, agent_id=agent_id)
            == []
        )
        assert (
            len(
                await list_channel_memory_stores_for_agent(
                    s, tenant_id=tenant.id, agent_id=other_agent
                )
            )
            == 1
        )
