"""Tests for notebook_host.pids_store — orphan-process recovery (D.7)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from notebook_host.pids_store import (
    PidRecord,
    load_pids,
    reap_orphans,
    record_from_process,
    save_pids,
)


def test_load_pids_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """No file → empty dict, never raises."""
    assert load_pids(tmp_path / "missing.json") == {}, (
        "missing pids file must produce an empty map, not an error"
    )


def test_load_pids_returns_empty_when_file_malformed(tmp_path: Path) -> None:
    """A truncated / non-JSON file produces empty map — we never crash on it."""
    path = tmp_path / "pids.json"
    path.write_text("{not valid json")
    assert load_pids(path) == {}, (
        "malformed file must produce empty map; the host can't trust unparseable state"
    )


def test_save_then_load_pids_round_trips(tmp_path: Path) -> None:
    """save_pids + load_pids preserves PidRecord fields."""
    path = tmp_path / "pids.json"
    records = {
        "slug-a": PidRecord(slug="slug-a", pid=1234, port=8100, started_at=1700000000.5),
        "slug-b": PidRecord(slug="slug-b", pid=5678, port=8101, started_at=1700000001.0),
    }
    save_pids(path, records)
    loaded = load_pids(path)
    assert loaded == records, "round-trip must preserve every PidRecord field exactly"


def test_save_pids_is_atomic(tmp_path: Path) -> None:
    """The .tmp file is renamed onto the target — no partial writes survive."""
    path = tmp_path / "pids.json"
    save_pids(path, {"x": PidRecord(slug="x", pid=1, port=8100, started_at=1.0)})
    assert path.exists(), "pids file must exist after save"
    assert not (tmp_path / "pids.json.tmp").exists(), (
        "no .tmp file should remain after a successful save"
    )
    # Sanity: the JSON is well-formed
    data = json.loads(path.read_text())
    assert data["x"]["pid"] == 1, "saved JSON must be readable"


def test_save_pids_creates_parent_directory(tmp_path: Path) -> None:
    """save_pids mkdir -p's the parent so callers don't have to."""
    path = tmp_path / "nested" / "deep" / "pids.json"
    save_pids(path, {})
    assert path.exists(), "save_pids should create missing parent directories"


def test_reap_orphans_returns_empty_when_no_file(tmp_path: Path) -> None:
    """Missing pids file → empty reap, no errors."""
    assert reap_orphans(tmp_path / "missing.json") == [], (
        "no prior state means nothing to reap; must not raise"
    )


def test_reap_orphans_skips_dead_pids(tmp_path: Path) -> None:
    """A record whose PID is already dead is skipped, not reported as reaped."""
    path = tmp_path / "pids.json"
    save_pids(
        path,
        {
            "ghost": PidRecord(
                # An unreachably high PID — guaranteed dead on any system.
                slug="ghost",
                pid=999_999,
                port=8100,
                started_at=1.0,
            )
        },
    )
    reaped = reap_orphans(path)
    assert reaped == [], "dead PIDs should not appear in the reaped list"
    # File must be cleared regardless — we never want stale entries to
    # be reaped twice on a subsequent restart.
    assert load_pids(path) == {}, "pids file must be cleared after sweep"


def test_reap_orphans_kills_live_process(tmp_path: Path) -> None:
    """A live PID listed in pids.json is SIGTERM'd and disappears."""
    # Spawn a real child we can prove was killed. Use a long-running
    # python so it doesn't exit on its own before reap runs.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        path = tmp_path / "pids.json"
        save_pids(
            path,
            {"live": PidRecord(slug="live", pid=child.pid, port=8100, started_at=time.time())},
        )

        reaped = reap_orphans(path, term_wait_seconds=3.0)

        assert [r.slug for r in reaped] == ["live"], (
            "a live PID listed in pids.json must be reported as reaped"
        )
        # The child should now be dead — give it a moment to exit cleanly
        # under SIGTERM and confirm via wait().
        rc = child.wait(timeout=5)
        assert rc is not None, "reaped child should have a returncode"
        assert load_pids(path) == {}, (
            "pids file must be cleared so the next host start sees clean state"
        )
    finally:
        # Defensive cleanup in case the test failed before reap_orphans
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child.pid, 9)


def test_record_from_process_accepts_float_timestamp() -> None:
    """The convenience constructor accepts the float NotebookProcess uses."""
    rec = record_from_process("s", 1234, 8100, 1700000000.5)
    assert rec.started_at == 1700000000.5, (
        "record_from_process must preserve the float epoch timestamp verbatim"
    )
