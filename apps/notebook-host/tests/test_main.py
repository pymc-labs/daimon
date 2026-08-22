"""Tests for notebook_host.main — blog respawn at boot and sweep self-heal."""

from __future__ import annotations

import asyncio
import runpy
import subprocess
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from notebook_host.jail import SlugPaths, get_slug_paths

# `--import-mode=importlib` (root pyproject.toml) doesn't add this directory
# to sys.path, and there's no `__init__.py` here (one would collide with the
# top-level `tests` package name already claimed by `packages/core/tests`).
# `runpy` loads `conftest.py` by file path without touching sys.path or
# sys.modules, so a full monorepo `pytest` collection can't collide with
# similar sys.path tricks in other adapters' tests (see
# `packages/adapters/mcp/tests/tools/conftest.py`).
set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]

pytestmark = pytest.mark.asyncio


def _make_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, alive: bool = True
) -> tuple[Any, list[tuple[str, SlugPaths, int, str]]]:
    """AdminState with a mode-recording stub spawner + monkeypatched wait_for_port."""
    import notebook_host.main as main_mod
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8600")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8603")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)

    calls: list[tuple[str, SlugPaths, int, str]] = []

    def spawner(
        slug: str,
        paths: SlugPaths,
        port: int,
        *,
        mode: str = "edit",
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]:
        calls.append((slug, paths, port, mode))
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
    paths = get_slug_paths(tmp_path, "pre-radar")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
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
    paths = get_slug_paths(tmp_path, "pre-radar")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
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
    paths = get_slug_paths(tmp_path, "ephemeral")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# x", encoding="utf-8")
    dead = state.make_process("ephemeral", 8601, _dead_proc(), mode="edit")
    state.processes["ephemeral"] = dead

    await _sweep_once(state)

    assert "ephemeral" not in state.processes, "a dead edit-mode notebook must still be reaped"
    assert not paths.notebook.exists(), "reaping an edit notebook deletes its source"


async def test_sweep_once_removes_attachments_and_workspace_when_reaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background sweep's reap branch removes the whole slug tree, not just the source.

    Pins the closed MEDIUM finding: the old reap branch only unlinked the
    source, leaking attachments and the workspace on the persistent volume
    forever once the slug was popped from state.processes.
    """
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, _calls = _make_state(tmp_path, monkeypatch)
    paths = get_slug_paths(tmp_path, "leaky")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# x", encoding="utf-8")
    paths.data.mkdir(parents=True, exist_ok=True)
    (paths.data / "big.nc").write_bytes(b"a" * 1024)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    (paths.workspace / "data").symlink_to(Path("..") / "data")
    (paths.workspace / "notebook.py").symlink_to(Path("..") / "notebook.py")

    dead = state.make_process("leaky", 8602, _dead_proc(), mode="edit")
    state.processes["leaky"] = dead

    await _sweep_once(state)

    assert "leaky" not in state.processes, "a dead edit-mode notebook must be reaped"
    assert paths.root.exists() is False, (
        "the whole slug tree — source, attachment, and workspace — must be gone after one sweep"
    )


async def test_sweep_once_reconciles_registered_blog_missing_from_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, calls = _make_state(tmp_path, monkeypatch)
    paths = get_slug_paths(tmp_path, "pre-orphan")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
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


async def test_sweep_skips_slug_unregistered_between_outer_scan_and_inner_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-heal loop's outer `load_blogs` is a stale candidate scan.

    If the slug was unregistered between that scan and the inner re-read taken
    under the lock, the sweep must not respawn it.
    """
    import notebook_host.main as main_mod
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, calls = _make_state(tmp_path, monkeypatch)
    call_count = 0
    real_load_blogs = main_mod.load_blogs

    def fake_load_blogs(path: Path) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            from notebook_host.blogs_store import BlogRecord

            return {"ghost-blog": BlogRecord(slug="ghost-blog", created_at=1.0)}
        return real_load_blogs(path)  # empty — nothing was ever actually registered

    monkeypatch.setattr(main_mod, "load_blogs", fake_load_blogs)

    mutated = await _sweep_once(state)

    assert calls == [], "the spawner must never be called for a slug that vanished under the lock"
    assert mutated is False, "skipping a stale candidate is not a mutation"


async def test_sweep_does_not_respawn_a_blog_deleted_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete that unregisters a blog while holding its slug lock must not be
    undone by a sweep tick that was already mid-flight for that slug.

    Driven deterministically: the test itself holds `state.lock_for(slug)` —
    the same lock object `delete_blog` acquires in production — forcing the
    sweep task to block on it exactly as it would against a real concurrent
    delete. While the sweep is blocked, the test performs the delete's
    registry/tree mutations (what `delete_blog` does under this same lock),
    then releases — letting the sweep's inner re-read observe the slug gone.
    """
    from notebook_host.blogs_store import BlogRecord, load_blogs, register_blog, unregister_blog
    from notebook_host.jail import remove_slug_tree
    from notebook_host.main import _sweep_once  # pyright: ignore[reportPrivateUsage]

    state, _calls = _make_state(tmp_path, monkeypatch)
    slug = "doomed"
    paths = get_slug_paths(tmp_path, slug)
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    register_blog(tmp_path / "blogs.json", BlogRecord(slug=slug, created_at=1.0))
    # Registered but absent from state.processes — a self-heal candidate,
    # exactly the shape a blog has right after delete_blog's unregister step
    # but before its tree removal finishes.
    assert slug not in state.processes

    lock = state.lock_for(slug)
    await lock.acquire()
    try:
        sweep_task = asyncio.create_task(_sweep_once(state))
        # Give the sweep task a turn so it reaches the (now-blocked) lock
        # acquire inside the self-heal loop.
        for _ in range(5):
            await asyncio.sleep(0)
        unregister_blog(tmp_path / "blogs.json", slug)
        remove_slug_tree(tmp_path, slug, uids_file=state.settings.resolved_uids_file)
    finally:
        lock.release()

    mutated = await sweep_task

    assert slug not in state.processes, "a blog deleted under the lock must not be respawned"
    assert slug not in load_blogs(tmp_path / "blogs.json"), "registry stays clear"
    assert not paths.root.exists(), "the deleted tree must not be recreated by the sweep"
    assert mutated is False, "skipping a deleted-under-the-lock slug is not a mutation"


# ─── fail-closed boot gate (D-05) ────────────────────────────────────────────
# These deliberately do NOT call set_unjailed_test_env — they pin the
# production default (fail-closed) rather than opting out of it.


async def test_create_app_lifespan_raises_when_jail_unavailable_and_no_break_glass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that cannot jail refuses to boot at all, per D-05."""
    import notebook_host.main as main_mod
    from notebook_host.config import load_settings
    from notebook_host.jail import JailUnavailableError

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    # allow_unjailed_spawn is deliberately left unset — production default (False).
    settings = load_settings(_env_file=None)
    monkeypatch.setattr(main_mod, "can_apply_jail", lambda: False)

    with pytest.raises(JailUnavailableError), TestClient(main_mod.create_app(settings)):
        pass


async def test_create_app_lifespan_boots_when_break_glass_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same unjailable host boots once the break-glass setting is on."""
    import notebook_host.main as main_mod
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)
    monkeypatch.setattr(main_mod, "can_apply_jail", lambda: False)

    with TestClient(main_mod.create_app(settings)) as client:
        resp = client.get("/health")
        assert resp.status_code == 200, "the break-glass opt-out should let an unjailable host boot"


# ─── boot migration (D-03: an existing flat deployment stays servable) ─────


async def test_create_app_lifespan_migrates_flat_layout_and_respawns_blog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host booted against a pre-upgrade flat data_dir respawns the blog from
    its migrated nested source.

    This is the test that proves D-03's actual promise: an existing blog
    keeps serving at the same URL across the upgrade.
    """
    import notebook_host.main as main_mod
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8610")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8613")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)

    (tmp_path / "pre-radar.py").write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    register_blog(tmp_path / "blogs.json", BlogRecord(slug="pre-radar", created_at=1.0))

    calls: list[tuple[str, SlugPaths, str]] = []

    def fake_spawn_marimo(
        slug: str,
        paths: SlugPaths,
        port: int,
        *,
        mode: str = "edit",
        sandbox: bool = False,
        rlimit_as_bytes: int | None = None,
        rlimit_cpu_seconds: int | None = None,
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]:
        calls.append((slug, paths, mode))
        proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.pid = 8888
        return proc  # type: ignore[return-value]

    async def fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(main_mod, "spawn_marimo", fake_spawn_marimo)
    monkeypatch.setattr(main_mod, "wait_for_port", fake_wait)

    with TestClient(main_mod.create_app(settings)):
        pass

    assert len(calls) == 1, "the migrated blog must be respawned exactly once at boot"
    slug, paths, mode = calls[0]
    assert slug == "pre-radar", "the respawned slug must be the one registered in blogs.json"
    assert mode == "run", "boot respawn of a registered blog must use run mode"
    assert paths.notebook == tmp_path / "pre-radar" / "notebook.py", (
        "the spawner must receive paths pointing at the migrated nested source, not the "
        "old flat file"
    )
    assert not (tmp_path / "pre-radar.py").exists(), (
        "the flat legacy source must be gone once the boot migration has run"
    )


async def test_create_app_lifespan_aborts_when_migration_raises_uid_pool_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that cannot allocate a uid for every legacy slug must abort the boot.

    Not respawn what it can and leave the rest — that would silently produce
    a half-isolated host.
    """
    import notebook_host.main as main_mod
    from notebook_host.config import load_settings
    from notebook_host.jail import UidPoolExhaustedError

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)

    def raising_migrate(*args: object, **kwargs: object) -> list[str]:
        raise UidPoolExhaustedError("pool exhausted")

    spawn_calls: list[object] = []

    def spy_spawn_marimo(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        spawn_calls.append((args, kwargs))
        proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        return proc  # type: ignore[return-value]

    monkeypatch.setattr(main_mod, "migrate_flat_layout", raising_migrate)
    monkeypatch.setattr(main_mod, "spawn_marimo", spy_spawn_marimo)

    with pytest.raises(UidPoolExhaustedError), TestClient(main_mod.create_app(settings)):
        pass

    assert spawn_calls == [], "no spawn may occur once the migration has raised"


def _dead_proc() -> subprocess.Popen[bytes]:
    proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = 0  # dead
    proc.pid = 9999
    return proc  # type: ignore[return-value]


async def test_failed_blog_respawn_clears_slug_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A respawn that never becomes ready must clear the slug's uv cache.

    Every failed sandboxed spawn leaves a fresh ephemeral uv environment in
    the slug's ``home/.cache/uv``; a blog stuck in the kill/retry loop grows
    that cache without bound until the data volume fills and every upload
    fails. A retry may pay a cold install; it must not leak disk.
    """
    import notebook_host.main as main_mod
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.main import _spawn_blog_process  # pyright: ignore[reportPrivateUsage]

    state, _calls = _make_state(tmp_path, monkeypatch, alive=False)

    async def _never_ready(port: int, slug: str, timeout_s: float) -> bool:
        return False

    monkeypatch.setattr(main_mod, "wait_for_port", _never_ready)
    paths = get_slug_paths(tmp_path, "pre-heavy")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
    register_blog(tmp_path / "blogs.json", BlogRecord(slug="pre-heavy", created_at=1.0))
    leaked_env = paths.home / ".cache" / "uv" / "environments-v2" / "leaked"
    leaked_env.mkdir(parents=True)
    (leaked_env / "pyvenv.cfg").write_text("home = /nowhere", encoding="utf-8")

    spawned = await _spawn_blog_process(state, "pre-heavy")

    assert spawned is False, "a spawn that never binds its port must report failure"
    assert not (paths.home / ".cache" / "uv").exists(), (
        "a failed respawn must remove the slug's uv cache so the retry loop cannot "
        "accumulate leaked ephemeral environments"
    )
    assert paths.notebook.exists(), "clearing the cache must not touch the blog source"
