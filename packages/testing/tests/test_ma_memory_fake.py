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
    assert page.data[0].content is None, "list defaults to view=basic (no content)"

    got = await client.beta.memory_stores.memories.retrieve(
        mem.id, memory_store_id=store.id
    )
    assert got.content is None, "retrieve without view=full must not return content"
    assert got.content_sha256 == mem.content_sha256

    got_full = await client.beta.memory_stores.memories.retrieve(
        mem.id, memory_store_id=store.id, view="full"
    )
    assert got_full.content == "alpha"

    archived = await client.beta.memory_stores.archive(store.id)
    assert archived.archived_at is not None

    deleted = await client.beta.memory_stores.delete(store.id)
    assert deleted.id == store.id
    assert store.id not in state.stores


async def test_list_with_segment_aware_path_prefix() -> None:
    """Verify path_prefix matching respects segment boundaries."""
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))

    store = await client.beta.memory_stores.create(
        name="test-store", description="test store for segment boundary"
    )

    # Create memories in two different "directories"
    await client.beta.memory_stores.memories.create(
        store.id, path="/notes/todo.md", content="task list"
    )
    await client.beta.memory_stores.memories.create(
        store.id, path="/notes-archive/old.md", content="archived notes"
    )

    # List with prefix "/notes/" should only return /notes/todo.md, not /notes-archive/old.md
    page = await client.beta.memory_stores.memories.list(store.id, path_prefix="/notes/")
    paths = [m.path for m in page.data]
    assert paths == ["/notes/todo.md"], f"Expected ['/notes/todo.md'], got {paths}"

    # List with prefix "/" should return both
    page = await client.beta.memory_stores.memories.list(store.id, path_prefix="/")
    paths = sorted([m.path for m in page.data])
    assert paths == ["/notes-archive/old.md", "/notes/todo.md"]
