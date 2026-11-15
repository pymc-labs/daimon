"""Host memory tools over the agent's existing memory stores (`daimon.core.host_memory`)."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.host_memory import (
    HostMemory,
    HostMemoryError,
    HostMemoryPlace,
    check_path,
    choose_store,
    replace_once,
)
from daimon.core.stores.agent_memory_stores import (
    get_channel_memory_store_id,
    get_memory_store_id,
    get_personal_memory_store_id,
)
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ALL_ON = {"shared_on": True, "personal_on": True, "channel_on": True}


def _place(
    tenant_id: uuid.UUID,
    *,
    hold: TurnHold | None = None,
    shared_thread: bool = False,
    read_only: bool = False,
) -> HostMemoryPlace:
    return HostMemoryPlace(
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        agent_name="daimon",
        account_id=uuid.uuid4(),
        hold=hold,
        shared_thread=shared_thread,
        read_only=read_only,
    )


def _hold() -> TurnHold:
    return TurnHold(platform="discord", channel_id="c1", client=False)


def test_choose_store_when_held_then_channel_store_only() -> None:
    place = _place(uuid.uuid4(), hold=_hold())
    assert choose_store(place, personal=False, **_ALL_ON) == "channel", "held turn: channel store"
    with pytest.raises(HostMemoryError):
        choose_store(place, personal=True, **_ALL_ON)
    with pytest.raises(HostMemoryError):
        choose_store(place, personal=False, shared_on=True, personal_on=True, channel_on=False)


def test_choose_store_when_shared_thread_then_no_personal_store() -> None:
    place = _place(uuid.uuid4(), shared_thread=True)
    assert choose_store(place, personal=False, **_ALL_ON) == "shared", "shared thread: shared"
    with pytest.raises(HostMemoryError):
        choose_store(place, personal=True, **_ALL_ON)


def test_choose_store_when_features_off_then_refused() -> None:
    place = _place(uuid.uuid4())
    assert choose_store(place, personal=True, **_ALL_ON) == "personal", "personal when asked"
    with pytest.raises(HostMemoryError):
        choose_store(place, personal=True, shared_on=True, personal_on=False, channel_on=True)
    with pytest.raises(HostMemoryError):
        choose_store(place, personal=False, shared_on=False, personal_on=True, channel_on=True)


@pytest.mark.parametrize("path", ["notes.md", "/a//b", "/a/../b", "/./a", "/a/.."])
def test_check_path_when_not_canonical_then_refused(path: str) -> None:
    with pytest.raises(HostMemoryError):
        check_path(path)


def test_replace_once_when_zero_or_many_then_refused() -> None:
    assert replace_once("a b", "a", "c") == "c b", "one occurrence is replaced"
    for content in ("b", "a a"):
        with pytest.raises(HostMemoryError):
            replace_once(content, "a", "c")


async def test_host_memory_when_shared_then_full_cycle_in_bound_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    place = _place(tenant.id)
    memory = HostMemory(client, db_session_factory, place, "shared")

    assert await memory.create("/notes/a.md", "hello world") == "Saved /notes/a.md."
    with pytest.raises(HostMemoryError):
        await memory.create("/notes/a.md", "again")
    await memory.str_replace("/notes/a.md", "world", "there")
    assert await memory.view("/notes/a.md") == "hello there", "replaced content reads back"
    assert await memory.view("/notes") == "/notes/a.md", "a directory lists its paths"
    store_id = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=place.agent_id)
    assert store_id is not None and len(state.memories[store_id]) == 1, "the agent's shared store"
    await memory.delete("/notes/a.md")
    assert state.memories[store_id] == [], "deleted from the shared store"


async def test_host_memory_when_read_only_then_writes_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = build_fake_anthropic(make_fake_memory_store_handler(FakeMemoryStoreState()))
    memory = HostMemory(client, db_session_factory, _place(tenant.id, read_only=True), "shared")
    with pytest.raises(HostMemoryError, match="read-only"):
        await memory.create("/a.md", "x")
    assert await memory.view("/") == "No memories under /", "reads still work"


async def test_host_memory_when_personal_or_held_then_own_store(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = build_fake_anthropic(make_fake_memory_store_handler(FakeMemoryStoreState()))
    personal = _place(tenant.id)
    await HostMemory(client, db_session_factory, personal, "personal").create("/p.md", "x")
    held = _place(tenant.id, hold=_hold())
    await HostMemory(client, db_session_factory, held, "channel").create("/c.md", "y")
    assert await get_personal_memory_store_id(
        db_session, tenant_id=tenant.id, agent_id=personal.agent_id, account_id=personal.account_id
    ), "personal store bound to the caller"
    assert await get_channel_memory_store_id(
        db_session, tenant_id=tenant.id, agent_id=held.agent_id, platform="discord", channel_id="c1"
    ), "channel store bound to the held channel"
    assert not await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=held.agent_id), (
        "a held turn never provisions the shared store"
    )
