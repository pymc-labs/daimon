"""Tests for notebook_host.consumed_store — durable, pruned single-use jti registry."""

from __future__ import annotations

from pathlib import Path

from notebook_host.consumed_store import burn_jti, load_consumed, prune_consumed, save_consumed


def test_load_consumed_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_consumed(tmp_path / "missing.json") == {}, (
        "missing registry must produce an empty map, not an error"
    )


def test_load_consumed_returns_empty_when_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "consumed.json"
    path.write_text("{not valid json")
    assert load_consumed(path) == {}, "malformed registry must produce an empty map"


def test_save_then_load_consumed_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "consumed.json"
    records = {"jti-a": 1700000100, "jti-b": 1700000200}
    save_consumed(path, records)
    assert load_consumed(path) == records, "round-trip must preserve every jti:exp pair"


def test_prune_consumed_drops_expired_and_keeps_unexpired(tmp_path: Path) -> None:
    records = {"expired": 100, "not-expired": 200, "boundary": 150}
    pruned = prune_consumed(records, now=150)
    assert pruned == {"not-expired": 200}, (
        "entries with exp <= now must be dropped; exp > now must be kept"
    )
    assert records == {"expired": 100, "not-expired": 200, "boundary": 150}, (
        "prune_consumed must not mutate its input"
    )


def test_burn_jti_returns_true_then_false_for_same_jti(tmp_path: Path) -> None:
    path = tmp_path / "consumed.json"
    first = burn_jti(path, "j1", exp=2_000_000_000, now=1_000)
    second = burn_jti(path, "j1", exp=2_000_000_000, now=1_000)
    assert first is True, "the first burn of a jti must succeed"
    assert second is False, "replaying the same jti must not burn again"


def test_burn_jti_prunes_expired_entries_so_the_file_does_not_grow_without_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consumed.json"
    now = 1_000
    burn_jti(path, "old-1", exp=now - 300, now=now)
    burn_jti(path, "old-2", exp=now - 200, now=now)
    burn_jti(path, "old-3", exp=now - 100, now=now)

    burn_jti(path, "fresh", exp=now + 300, now=now)

    assert len(load_consumed(path)) == 1, (
        "burning a new jti must prune every already-expired entry, not just skip them"
    )
    assert load_consumed(path) == {"fresh": now + 300}
