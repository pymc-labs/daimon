"""Tests for notebook_host.main — blog respawn at boot and sweep self-heal."""

from __future__ import annotations

import subprocess
import unittest.mock
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


def _make_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, alive: bool = True
) -> tuple[Any, list[tuple[str, Path, int, str]]]:
    """AdminState with a mode-recording stub spawner + monkeypatched wait_for_port."""
    import notebook_host.main as main_mod
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8600")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8603")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")
    settings = load_settings(_env_file=None)

    calls: list[tuple[str, Path, int, str]] = []

    def spawner(
        slug: str, file_path: Path, port: int, *, mode: str = "edit"
    ) -> subprocess.Popen[bytes]:
        calls.append((slug, file_path, port, mode))
        proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None if alive else 0
        proc.pid = 7777
        return proc  # type: ignore[return-value]

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(main_mod, "wait_for_port", _fake_wait)
    state = AdminState(settings=settings, processes={}, spawner=spawner, validator=None)
    return state, calls


async def test_respawn_registered_blogs_spawns_run_mode_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.main import _respawn_registered_blogs  # pyright: ignore[reportPrivateUsage]

    state, calls = _make_state(tmp_path, monkeypatch)
    (tmp_path / "pre-radar.py").write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    register_blog(tmp_path / "blogs.json", BlogRecord(slug="pre-radar", created_at=1.0))

    respawned = await _respawn_registered_blogs(state)

    assert respawned == ["pre-radar"], "boot must respawn the registered blog"
    assert calls[0][3] == "run", "boot respawn must use run mode"
    assert "pre-radar" in state.processes, "respawned blog must be tracked"


async def test_respawn_skips_blog_with_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.main import _respawn_registered_blogs  # pyright: ignore[reportPrivateUsage]

    state, _calls = _make_state(tmp_path, monkeypatch)
    register_blog(tmp_path / "blogs.json", BlogRecord(slug="pre-ghost", created_at=1.0))
    # No pre-ghost.py on disk.

    respawned = await _respawn_registered_blogs(state)

    assert respawned == [], "a registered blog with no source file must be skipped, not crash"
    assert "pre-ghost" not in state.processes


async def test_sweep_once_respawns_dead_blog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, calls = _make_state(tmp_path, monkeypatch, alive=True)
    (tmp_path / "pre-radar.py").write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    # Place a DEAD run-mode process directly in state to simulate a crashed kernel.
    dead = state.make_process(
        "pre-radar",
        8600,
        _dead_proc(),
        mode="run",
    )
    state.processes["pre-radar"] = dead

    await _sweep_once(state)

    assert "pre-radar" in state.processes, "a dead blog must be respawned, not dropped"
    assert state.processes["pre-radar"].is_alive(), "respawned blog must be alive"
    assert calls[-1][3] == "run", "self-heal respawn must use run mode"


async def test_sweep_once_reaps_dead_edit_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, _calls = _make_state(tmp_path, monkeypatch)
    (tmp_path / "ephemeral.py").write_text("# x", encoding="utf-8")
    dead = state.make_process("ephemeral", 8601, _dead_proc(), mode="edit")
    state.processes["ephemeral"] = dead

    await _sweep_once(state)

    assert "ephemeral" not in state.processes, "a dead edit-mode notebook must still be reaped"
    assert not (tmp_path / "ephemeral.py").exists(), "reaping an edit notebook deletes its source"


async def test_sweep_once_reconciles_registered_blog_missing_from_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, calls = _make_state(tmp_path, monkeypatch)
    (tmp_path / "pre-orphan.py").write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    # Registered in blogs.json, but NOT in state.processes (a prior respawn failed).
    register_blog(tmp_path / "blogs.json", BlogRecord(slug="pre-orphan", created_at=1.0))
    assert "pre-orphan" not in state.processes

    mutated = await _sweep_once(state)

    assert "pre-orphan" in state.processes, (
        "a registered blog absent from processes must be respawned by the sweep"
    )
    assert state.processes["pre-orphan"].is_alive(), "reconciled blog must be alive"
    assert calls[-1][3] == "run", "reconcile respawn must use run mode"
    assert mutated is True, "respawning an orphaned blog mutates state"


def _dead_proc() -> subprocess.Popen[bytes]:
    proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = 0  # dead
    proc.pid = 9999
    return proc  # type: ignore[return-value]
