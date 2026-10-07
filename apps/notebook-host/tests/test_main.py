"""Tests for notebook_host.main — the boot gate and the boot migration.

Starting, stopping and expiring registered notebooks is covered in
test_lazy_spawn.py.
"""

from __future__ import annotations

import runpy
import subprocess
import unittest.mock
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from notebook_host.jail import SlugPaths

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


async def test_create_app_lifespan_migrates_flat_layout_and_serves_blog_on_visit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host booted against a pre-upgrade flat data_dir serves the blog from
    its migrated nested source on the first visit.

    This is the test that proves D-03's actual promise: an existing blog
    keeps serving at the same URL across the upgrade.
    """
    import notebook_host.lazy_spawn as lazy_mod
    import notebook_host.main as main_mod
    from notebook_host.blogs_store import BlogRecord, register_blog
    from notebook_host.config import load_settings
    from notebook_host.lazy_spawn import ensure_running

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
        access_token: str = "",
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

    async def fake_wait(port: int, slug: str, timeout_s: float, *, access_token: str = "") -> bool:
        return True

    monkeypatch.setattr(main_mod, "spawn_marimo", fake_spawn_marimo)
    monkeypatch.setattr(lazy_mod, "wait_for_port", fake_wait)

    app = main_mod.create_app(settings)
    with TestClient(app):
        assert calls == [], "boot starts nothing; a registered notebook starts on a visit"
        np = await ensure_running(app.state.admin_state, "pre-radar", now=2.0)
        assert np is not None, "the migrated blog is still registered and starts"

    assert len(calls) == 1, "the migrated blog must be started exactly once"
    slug, paths, mode = calls[0]
    assert slug == "pre-radar", "the started slug must be the one registered in blogs.json"
    assert mode == "run", "a registered blog starts as the read-only app"
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
