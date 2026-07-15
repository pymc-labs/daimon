"""Tests for notebook_host.blogs_store — durable persistent-blog registry."""

from __future__ import annotations

from pathlib import Path

from notebook_host.blogs_store import (
    BlogRecord,
    load_blogs,
    register_blog,
    save_blogs,
    unregister_blog,
)


def test_load_blogs_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_blogs(tmp_path / "missing.json") == {}, (
        "missing registry must produce an empty map, not an error"
    )


def test_load_blogs_returns_empty_when_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "blogs.json"
    path.write_text("{not valid json")
    assert load_blogs(path) == {}, "malformed registry must produce an empty map"


def test_save_then_load_blogs_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "blogs.json"
    records = {
        "pre-radar": BlogRecord(slug="pre-radar", created_at=1700000000.5, title="Radar"),
        "pre-funnel": BlogRecord(slug="pre-funnel", created_at=1700000001.0),
    }
    save_blogs(path, records)
    assert load_blogs(path) == records, "round-trip must preserve every BlogRecord field"


def test_register_blog_upserts(tmp_path: Path) -> None:
    path = tmp_path / "blogs.json"
    register_blog(path, BlogRecord(slug="pre-a", created_at=1.0))
    register_blog(path, BlogRecord(slug="pre-b", created_at=2.0))
    register_blog(path, BlogRecord(slug="pre-a", created_at=3.0, title="updated"))
    loaded = load_blogs(path)
    assert set(loaded) == {"pre-a", "pre-b"}, "register must add without dropping siblings"
    assert loaded["pre-a"].created_at == 3.0, "re-registering a slug overwrites its record"


def test_unregister_blog_drops_only_that_slug(tmp_path: Path) -> None:
    path = tmp_path / "blogs.json"
    register_blog(path, BlogRecord(slug="pre-a", created_at=1.0))
    register_blog(path, BlogRecord(slug="pre-b", created_at=2.0))
    unregister_blog(path, "pre-a")
    assert set(load_blogs(path)) == {"pre-b"}, "unregister must drop only the named slug"


def test_unregister_missing_slug_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "blogs.json"
    register_blog(path, BlogRecord(slug="pre-b", created_at=2.0))
    unregister_blog(path, "pre-absent")  # must not raise
    assert set(load_blogs(path)) == {"pre-b"}, "no-op unregister must not disturb unrelated entries"
