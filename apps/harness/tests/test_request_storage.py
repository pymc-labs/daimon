"""Large bundles stay shared and terminal claims retain durable idempotency."""

import json
import sqlite3

import pytest
from daimon_harness.journal import ConflictError, Journal
from daimon_harness.request_storage import canonical
from mux.drivers.selfhosted.wire import TurnRequest

from .conftest import turn


def request(number=0, session="session-1"):
    data = turn(turn_id=f"turn-{number}", key=f"key-{number}")
    data.update(
        session_id=session,
        instructions="prompt" * 10_000,
        skills=[{"name": "large", "files": {"SKILL.md": "bundle-content" * 80_000}}],
    )
    return TurnRequest.model_validate(data)


def test_active_bundles_shared_then_reclaimed_without_losing_replay_identity(tmp_path):
    journal = Journal(tmp_path)
    first, second = request(), request(1, "session-2")
    assert journal.claim(first)
    assert journal.claim(second)
    assert journal.db.execute("SELECT COUNT(*) FROM skill_bundles").fetchone() == (1,)
    assert journal.find(first.turn_id) == first
    assert journal.find(second.turn_id) == second
    assert journal.turns("session-2") == [second]
    assert all(len(row[0]) < 100_000 for row in journal.db.execute("SELECT request FROM turns"))
    journal.append(first, "turn_ended", {"outcome": "completed"})
    assert journal.db.execute("SELECT COUNT(*) FROM skill_bundles").fetchone() == (1,)
    assert journal.find(second.turn_id) == second
    journal.close()
    journal = Journal(tmp_path)
    try:
        assert journal.is_replay(first)
        assert journal.find(second.turn_id) == second
        journal.append(second, "turn_ended", {"outcome": "completed"})
        assert journal.db.execute("SELECT COUNT(*) FROM skill_bundles").fetchone() == (0,)
        assert journal.db.execute("SELECT COUNT(*) FROM turn_skills").fetchone() == (0,)
        assert journal.is_replay(second)
        for changed in [
            first.model_copy(update={"instructions": "changed"}),
            first.model_copy(update={"skills": []}),
            first.model_copy(update={"session_id": "other"}),
            first.model_copy(update={"turn_id": "other"}),
        ]:
            with pytest.raises(ConflictError):
                journal.claim(changed)
    finally:
        journal.close()


def test_repeated_large_turns_keep_compact_claims_and_event_history(tmp_path):
    journal = Journal(tmp_path)
    try:
        for number in range(48):
            current = request(number)
            assert journal.claim(current)
            journal.append(current, "turn_ended", {"outcome": "completed"})
        assert len(journal.events("session-1")) == 48
        assert len(journal.turns()) == 48
        for number in range(48):
            assert journal.is_replay(request(number))
        # More than 48 MiB of submitted bundles, independent of compression.
        assert sum(len(row[0]) for row in journal.db.execute("SELECT request FROM turns")) < 32_000
        assert (journal.root / "turns.sqlite3").stat().st_size < 2 * 1024 * 1024
        assert (journal.root / "turns.sqlite3-wal").stat().st_size == 0
    finally:
        journal.close()


def test_migrates_inline_requests_and_reclaims_old_pages(tmp_path):
    root = tmp_path / ".daimon"
    root.mkdir()
    db = sqlite3.connect(root / "turns.sqlite3")
    db.execute(
        "CREATE TABLE turns (turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, ended INTEGER NOT NULL DEFAULT 0)"
    )
    for number in range(8):
        current = request(number, f"session-{number}")
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?)",
            (
                current.turn_id,
                current.session_id,
                current.idempotency_key,
                canonical(current),
                int(number != 7),
            ),
        )
    db.commit()
    before = (root / "turns.sqlite3").stat().st_size
    db.close()
    journal = Journal(tmp_path)
    try:
        assert (root / "turns.sqlite3").stat().st_size < before / 3
        for number in range(8):
            current = request(number, f"session-{number}")
            assert journal.is_replay(current)
        assert journal.find("turn-7") == request(7, "session-7")
        assert journal.find("turn-0").skills == []
        assert journal.db.execute("SELECT COUNT(*) FROM skill_bundles").fetchone() == (1,)
        assert journal.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        journal.close()


def test_terminal_event_index_crash_window_also_compacts_request(tmp_path):
    journal = Journal(tmp_path)
    current = request()
    journal.claim(current)
    # The session event committed; the process died before updating the global index.
    event = {
        "seq": 1,
        "session_id": current.session_id,
        "turn_id": current.turn_id,
        "at": "2026-10-21T00:00:00Z",
        "type": "turn_ended",
        "data": {"outcome": "interrupted"},
    }
    with journal.connection(current.session_id) as db:
        db.execute(
            "INSERT INTO events VALUES (1, ?, ?, 'turn_ended')",
            (json.dumps(event), current.turn_id),
        )
    journal.close()
    journal = Journal(tmp_path)
    try:
        assert journal.ended(current.turn_id)
        assert journal.is_replay(current)
        assert journal.find(current.turn_id).skills == []
        assert journal.db.execute("SELECT COUNT(*) FROM skill_bundles").fetchone() == (0,)
    finally:
        journal.close()


def test_bundle_references_preserve_mixed_skill_order_and_binary_bytes(tmp_path):
    current = request().model_copy(update={"skills": []})
    body = current.model_dump(mode="json")
    body["skills"] = [
        {"name": "remote", "url": "https://example.com/skills/archive.zip"},
        {
            "name": "binary",
            "files": {"SKILL.md": "instructions"},
            "binary_files": {"asset.bin": "AP8="},
        },
        {"name": "last", "files": {"SKILL.md": "last"}},
    ]
    current = TurnRequest.model_validate(body)
    journal = Journal(tmp_path)
    journal.claim(current)
    journal.close()
    journal = Journal(tmp_path)
    try:
        assert journal.find(current.turn_id) == current
        assert journal.turns() == [current]
        assert journal.is_replay(current)
    finally:
        journal.close()
