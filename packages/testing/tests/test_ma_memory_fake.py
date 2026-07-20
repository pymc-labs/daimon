"""The memory-store fake must satisfy real SDK parsing end-to-end."""

from __future__ import annotations

import pytest
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)

pytestmark = pytest.mark.asyncio


async def test_create_seed_list_retrieve_archive_delete_roundtrip() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    store = await client.beta.memory_stores.create(
        name="daimon test-agent", description="test store", metadata={"daimon_tenant": "t1"}
    )
    assert store.id.startswith("memstore_")
    assert store.metadata == {"daimon_tenant": "t1"}

    mem = await client.beta.memory_stores.memories.create(
        store.id, path="/notes/a.md", content="alpha"
    )
    assert mem.path == "/notes/a.md"

    page = await client.beta.memory_stores.memories.list(store.id, path_prefix="/")
    assert [m.path for m in page.data] == ["/notes/a.md"]

    got = await client.beta.memory_stores.memories.retrieve(
        mem.id, memory_store_id=store.id
    )
    assert got.content == "alpha"

    archived = await client.beta.memory_stores.archive(store.id)
    assert archived.archived_at is not None

    deleted = await client.beta.memory_stores.delete(store.id)
    assert deleted.id == store.id
    assert store.id not in state.stores
