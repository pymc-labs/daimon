"""Tests for the disk-backed FileStore used by media MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daimon.adapters.mcp.file_store import (
    MAX_FILE_SIZE,
    TTL_SECONDS,
    FileStore,
)


def test_put_and_get_roundtrips_bytes(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    handle = store.put(data=b"hi there", mime_type="text/plain", title="hello")
    fetched = store.get(handle.id)
    assert fetched.data == b"hi there"
    assert fetched.title == "hello", "title must be preserved as metadata"
    assert fetched.display_filename == "hello.txt", (
        "display_filename should be sanitized title plus mime-derived extension"
    )
    assert fetched.content_type == "text/plain"
    assert handle.id.endswith(".txt"), "minted id should include the mime extension"


def test_put_two_files_with_same_title_get_distinct_handles(tmp_path: Path) -> None:
    """Server-minted handles must not collide on duplicate titles."""
    store = FileStore(base_dir=tmp_path)
    h1 = store.put(data=b"first", mime_type="audio/mpeg", title="report")
    h2 = store.put(data=b"second", mime_type="audio/mpeg", title="report")
    assert h1.id != h2.id, "two puts with identical titles must produce distinct handle ids"
    assert store.get(h1.id).data == b"first", "first put's bytes must survive"
    assert store.get(h2.id).data == b"second", "second put's bytes must survive"


def test_get_raises_keyerror_for_missing_handle(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    with pytest.raises(KeyError):
        store.get("nope.txt")


def test_get_rejects_traversal_in_handle_id(tmp_path: Path) -> None:
    """A caller can't trick the store into reading outside its base dir
    by passing a forged handle id."""
    store = FileStore(base_dir=tmp_path)
    for bad in ("../escape.txt", "sub/dir.txt", "back\\slash.txt"):
        with pytest.raises(ValueError, match="invalid character"):
            store.get(bad)


def test_put_rejects_oversize_file(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    with pytest.raises(ValueError, match="per-file limit"):
        store.put(
            data=b"x" * (MAX_FILE_SIZE + 1),
            mime_type="application/octet-stream",
            title="big",
        )


def test_put_evicts_expired_entries(tmp_path: Path) -> None:
    """Handles older than TTL are removed on subsequent put/get/list calls."""
    fake_now = datetime(2026, 1, 1, tzinfo=UTC)

    def now() -> datetime:
        return fake_now

    store = FileStore(base_dir=tmp_path, now=now)
    handle = store.put(data=b"a", mime_type="text/plain", title="a")
    fake_now = fake_now + timedelta(seconds=TTL_SECONDS + 1)
    with pytest.raises(KeyError):
        store.get(handle.id)


def test_list_available_returns_minted_ids(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    h1 = store.put(data=b"a", mime_type="text/plain", title="a")
    h2 = store.put(data=b"b", mime_type="text/plain", title="b")
    assert sorted(store.list_available()) == sorted([h1.id, h2.id]), (
        "list_available returns the server-minted handle ids, not titles"
    )


def test_delete_removes_both_data_and_meta(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    handle = store.put(data=b"a", mime_type="text/plain", title="a")
    store.delete(handle.id)
    assert store.list_available() == []
    with pytest.raises(KeyError):
        store.get(handle.id)


def test_display_filename_sanitizes_unsafe_chars(tmp_path: Path) -> None:
    """Path separators and weird unicode in titles must not leak into the
    user-visible filename Discord renders."""
    store = FileStore(base_dir=tmp_path)
    handle = store.put(data=b"a", mime_type="image/png", title="../weird /name")
    fetched = store.get(handle.id)
    assert "/" not in fetched.display_filename, "display filename must not contain '/'"
    assert ".." not in fetched.display_filename, "display filename must not contain '..'"
    assert fetched.display_filename.endswith(".png"), "extension follows mime_type"
