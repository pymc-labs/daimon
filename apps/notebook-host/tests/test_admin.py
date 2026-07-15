"""Tests for notebook_host.admin router.

All tests use TestClient with a stubbed Spawner so no real marimo subprocesses
are spawned. `wait_for_port` is monkeypatched at the module level to return True
(or False for the timeout test).
"""

from __future__ import annotations

import subprocess
import time
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

AUTH = "Bearer test-secret"
NO_AUTH = "Bearer wrong-secret"


def _make_stub_spawner(
    pid: int = 12345,
) -> tuple[unittest.mock.MagicMock, list[tuple[str, Path, int]]]:
    """Return (stub_spawner, calls_log).

    The stub records every (slug, file_path, port) call and returns a Popen mock.
    """
    calls: list[tuple[str, Path, int]] = []

    def spawner(
        slug: str, file_path: Path, port: int, *, mode: str = "edit"
    ) -> subprocess.Popen[bytes]:
        calls.append((slug, file_path, port))
        mock_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None  # alive
        mock_proc.pid = pid
        return mock_proc  # type: ignore[return-value]

    stub = unittest.mock.MagicMock(side_effect=spawner)
    return stub, calls


def _make_test_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_for_port_returns: bool = True,
    validator: Any = None,
) -> tuple[TestClient, Any, unittest.mock.MagicMock]:
    """Build a FastAPI app with stub spawner and monkeypatched wait_for_port.

    Pass ``validator`` (a callable ``(slug, file_path) -> ValidationResult``) to
    exercise the pre-publish validation path; the default of None disables it.

    Returns (client, admin_state, stub_spawner).
    """
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")

    settings = load_settings(_env_file=None)
    stub_spawner, _ = _make_stub_spawner()

    # Monkeypatch wait_for_port at the admin module level
    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return wait_for_port_returns

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    processes: dict[str, Any] = {}
    state = AdminState(
        settings=settings, processes=processes, spawner=stub_spawner, validator=validator
    )

    app = FastAPI()
    app.include_router(create_admin_router(state))

    return TestClient(app, raise_server_exceptions=True), state, stub_spawner


# ─── /health — no bearer required ────────────────────────────────────────────


def test_health_returns_200_without_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health returns 200 without any Authorization header."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200, "health should return 200 without auth"
    body = resp.json()
    assert body["status"] == "ok", "health body should have status ok"
    assert "port_pool" in body, "health body should include port_pool"
    assert body["port_pool"]["capacity"] == 2, "capacity should be end - start + 1 = 2"
    assert "in_use" in body["port_pool"], "port_pool should include in_use"


# ─── /admin/notebooks PUT ────────────────────────────────────────────────────


def test_put_notebook_without_bearer_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /admin/notebooks/{slug} without bearer returns 401."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put("/admin/notebooks/myslug", json={"source": "x = 1"})
    assert resp.status_code == 401, "missing bearer should return 401"


def test_put_notebook_with_wrong_bearer_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /admin/notebooks/{slug} with wrong bearer returns 401."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put(
        "/admin/notebooks/myslug",
        json={"source": "x = 1"},
        headers={"Authorization": NO_AUTH},
    )
    assert resp.status_code == 401, "wrong bearer should return 401"


def test_put_notebook_with_valid_bearer_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /admin/notebooks/{slug} with valid bearer returns 200 with expected keys."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put(
        "/admin/notebooks/myslug",
        json={"source": "x = 1"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, "valid bearer should return 200"
    body = resp.json()
    assert body["slug"] == "myslug", "response should include the slug"
    assert "url" in body, "response should include url"
    assert "port" in body, "response should include port"
    assert "pid" in body, "response should include pid"
    assert "size_bytes" in body, "response should include size_bytes"
    assert "subprocess_ttl_seconds" in body, "response should include subprocess_ttl_seconds"
    assert "expires_at" in body, "response should include expires_at"
    # expires_at must parse as an ISO-8601 timestamp with timezone info.
    parsed = datetime.fromisoformat(body["expires_at"])
    assert parsed.tzinfo is not None, "expires_at should be timezone-aware"
    # And it should be in the future, ttl seconds from now (give a 5s slack).
    delta = (parsed - datetime.now(UTC)).total_seconds()
    assert 0 < delta <= body["subprocess_ttl_seconds"] + 5, (
        f"expires_at should be ~ttl seconds in the future; delta={delta}"
    )


def test_put_notebook_writes_file_to_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /admin/notebooks/{slug} writes <data_dir>/<slug>.py with posted source."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    source = "import marimo\napp = marimo.App()\n"
    client.put(
        "/admin/notebooks/nb1",
        json={"source": source},
        headers={"Authorization": AUTH},
    )
    written = (tmp_path / "nb1.py").read_text()
    assert written == source, "file should contain the posted source verbatim"


def test_put_existing_slug_kills_old_and_respawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT to an existing slug kills the prior process and re-spawns."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")

    settings = load_settings(_env_file=None)
    stub_spawner, calls = _make_stub_spawner()

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    # Track kill calls
    killed: list[Any] = []
    monkeypatch.setattr(admin_mod, "kill", lambda np: killed.append(np))

    processes: dict[str, Any] = {}
    state = AdminState(settings=settings, processes=processes, spawner=stub_spawner)

    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    # First PUT
    client.put("/admin/notebooks/nb1", json={"source": "v1"}, headers={"Authorization": AUTH})
    assert len(calls) == 1, "spawner should be called once after first PUT"
    old_np = state.processes.get("nb1")
    assert old_np is not None, "state should contain nb1 after first PUT"

    # Second PUT — should kill old and respawn
    client.put("/admin/notebooks/nb1", json={"source": "v2"}, headers={"Authorization": AUTH})
    assert len(calls) == 2, "spawner should be called again on overwrite"
    assert len(killed) == 1, "kill should be called once for the old process"


def test_put_invalid_slug_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /admin/notebooks/{slug} with an empty slug segment returns 400 or 404.

    Slugs containing '/' are stripped by URL routing before reaching the handler.
    We test via a NUL-byte url-encoded slug to verify safe_slug rejects it.
    Note: empty slug ('') can't be sent as a path param (404 from router);
    the safe_slug function itself is tested in test_lifecycle.py for all invalid cases.
    The admin router delegates to safe_slug, so we verify the 400 path via a
    slug value that reaches the handler but is rejected by safe_slug.
    """
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    # A slug with a NUL byte url-encoded: %00 — reaches the handler, rejected by safe_slug
    resp = client.put(
        "/admin/notebooks/a%00b",
        json={"source": "x"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 400, "slug with NUL byte should return 400"


def test_put_notebook_oversize_source_returns_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT with source larger than max_source_bytes returns 413."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__MAX_SOURCE_BYTES", "1024")  # 1 KiB cap for the test
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    big = "x" * 2048
    resp = client.put(
        "/admin/notebooks/big-nb",
        json={"source": big},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 413, f"oversize source should return 413, got {resp.status_code}"
    assert "max_source_bytes" in resp.text, "detail should reference the cap"
    assert not (tmp_path / "big-nb.py").exists(), "no file should be written on size-cap reject"


def test_put_notebook_under_size_cap_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT just under the cap still succeeds."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__MAX_SOURCE_BYTES", "1024")
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    just_fits = "y" * 1024
    resp = client.put(
        "/admin/notebooks/fits",
        json={"source": just_fits},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, f"under-cap source should succeed, got {resp.status_code}"


def test_put_notebook_timeout_returns_504(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /admin/notebooks/{slug} returns 504 when subprocess does not become ready."""
    client, state, _ = _make_test_app(tmp_path, monkeypatch, wait_for_port_returns=False)
    resp = client.put(
        "/admin/notebooks/slow-nb",
        json={"source": "x = 1"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 504, "unready subprocess should return 504"
    # Registry entry should be removed
    assert "slow-nb" not in state.processes, "registry should not contain the slug after timeout"


# ─── /admin/notebooks DELETE ─────────────────────────────────────────────────


def test_delete_notebook_returns_204(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE /admin/notebooks/{slug} returns 204, removes file, clears registry."""
    client, state, _ = _make_test_app(tmp_path, monkeypatch)

    # First PUT to create
    client.put("/admin/notebooks/del-me", json={"source": "y = 2"}, headers={"Authorization": AUTH})
    assert "del-me" in state.processes, "state should contain del-me after PUT"
    assert (tmp_path / "del-me.py").exists(), "file should exist after PUT"

    # Now DELETE
    resp = client.delete("/admin/notebooks/del-me", headers={"Authorization": AUTH})
    assert resp.status_code == 204, "DELETE should return 204"
    assert "del-me" not in state.processes, "registry should not contain del-me after DELETE"
    assert not (tmp_path / "del-me.py").exists(), "file should be removed after DELETE"


def test_delete_notebook_rmtrees_data_and_workspace_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE also removes <slug>.data/ and <slug>_workspace/ (with their contents)."""
    client, state, _ = _make_test_app(tmp_path, monkeypatch)

    client.put("/admin/notebooks/del-me", json={"source": "y=2"}, headers={"Authorization": AUTH})
    # The stub spawner doesn't run _prepare_workspace, so simulate the dirs.
    data_dir = tmp_path / "del-me.data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "sales.csv").write_bytes(b"a,b\n1,2\n")
    workspace = tmp_path / "del-me_workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "data").symlink_to(Path("..") / "del-me.data")
    (workspace / "del-me.py").symlink_to(Path("..") / "del-me.py")

    resp = client.delete("/admin/notebooks/del-me", headers={"Authorization": AUTH})
    assert resp.status_code == 204, "DELETE should return 204"
    assert not (tmp_path / "del-me.py").exists(), "source file should be removed"
    assert not data_dir.exists(), "<slug>.data/ should be rmtreed (with attachments)"
    assert not workspace.exists(), "<slug>_workspace/ should be rmtreed (with symlinks)"
    assert "del-me" not in state.processes


def test_delete_notebook_when_dirs_missing_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE succeeds even when data/workspace dirs don't exist (ignore_errors=True)."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)

    # No PUT — nothing exists for this slug.
    resp = client.delete("/admin/notebooks/never-existed", headers={"Authorization": AUTH})
    assert resp.status_code == 204, (
        "DELETE on a never-existed slug should still 204 (idempotent rmtree)"
    )


# ─── /admin/notebooks GET (list) ─────────────────────────────────────────────


def test_list_notebooks_returns_sorted_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /admin/notebooks returns sorted list with required keys."""
    client, _, _ = _make_test_app(tmp_path, monkeypatch)

    client.put("/admin/notebooks/beta", json={"source": "b"}, headers={"Authorization": AUTH})
    client.put("/admin/notebooks/alpha", json={"source": "a"}, headers={"Authorization": AUTH})

    resp = client.get("/admin/notebooks", headers={"Authorization": AUTH})
    assert resp.status_code == 200, "list should return 200"
    body = resp.json()
    notebooks = body["notebooks"]
    assert len(notebooks) == 2, "should list both notebooks"
    slugs = [nb["slug"] for nb in notebooks]
    assert slugs == sorted(slugs), "notebooks should be returned sorted by slug"
    for nb in notebooks:
        assert "slug" in nb, "each entry should have slug"
        assert "url" in nb, "each entry should have url"
        assert "port" in nb, "each entry should have port"
        assert "pid" in nb, "each entry should have pid"
        assert "alive" in nb, "each entry should have alive"
        assert "age_s" in nb, "each entry should have age_s"


# ─── /admin/sweep POST ───────────────────────────────────────────────────────


def test_sweep_reaps_dead_and_ttl_past_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/sweep reaps dead processes and TTL-past processes."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings
    from notebook_host.lifecycle import NotebookProcess

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8599")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")

    settings = load_settings(_env_file=None)

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)
    monkeypatch.setattr(admin_mod, "kill", lambda np: None)  # no-op kill

    processes: dict[str, Any] = {}
    state = AdminState(settings=settings, processes=processes, spawner=_make_stub_spawner()[0])

    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    # Manually inject a dead process
    dead_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    dead_proc.poll.return_value = 1  # dead
    dead_proc.pid = 11111
    (tmp_path / "dead-nb.py").write_text("x", encoding="utf-8")
    processes["dead-nb"] = NotebookProcess(
        slug="dead-nb",
        port=8500,
        process=dead_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
    )

    # Inject a TTL-past alive process (started_at very far in the past)
    alive_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    alive_proc.poll.return_value = None  # alive
    alive_proc.pid = 22222
    (tmp_path / "old-nb.py").write_text("y", encoding="utf-8")
    processes["old-nb"] = NotebookProcess(
        slug="old-nb",
        port=8501,
        process=alive_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
        started_at=time.time() - settings.subprocess_ttl_seconds - 1,
    )

    # Inject a healthy process that should NOT be reaped
    good_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    good_proc.poll.return_value = None
    good_proc.pid = 33333
    (tmp_path / "good-nb.py").write_text("z", encoding="utf-8")
    processes["good-nb"] = NotebookProcess(
        slug="good-nb",
        port=8502,
        process=good_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
    )

    resp = client.post("/admin/sweep", headers={"Authorization": AUTH})
    assert resp.status_code == 200, "sweep should return 200"
    body = resp.json()
    reaped_slugs = [r["slug"] for r in body["reaped"]]
    assert "dead-nb" in reaped_slugs, "dead process should be reaped"
    assert "old-nb" in reaped_slugs, "TTL-past process should be reaped"
    assert "good-nb" not in reaped_slugs, "healthy process should not be reaped"
    assert "dead-nb" not in state.processes, "dead-nb should be removed from registry"
    assert "old-nb" not in state.processes, "old-nb should be removed from registry"
    assert "good-nb" in state.processes, "good-nb should remain in registry"


def test_sweep_rmtrees_data_and_workspace_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/sweep also rmtrees <slug>.data/ and <slug>_workspace/ for reaped slugs."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings
    from notebook_host.lifecycle import NotebookProcess

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8599")
    monkeypatch.setattr(admin_mod, "kill", lambda np: None)

    settings = load_settings(_env_file=None)
    processes: dict[str, Any] = {}
    state = AdminState(settings=settings, processes=processes, spawner=_make_stub_spawner()[0])
    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    # Inject a dead slug with full filesystem shape (source + data + workspace).
    dead_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    dead_proc.poll.return_value = 1
    dead_proc.pid = 11111
    (tmp_path / "dead-nb.py").write_text("x", encoding="utf-8")
    data_dir = tmp_path / "dead-nb.data"
    data_dir.mkdir()
    (data_dir / "x.csv").write_bytes(b"a")
    workspace = tmp_path / "dead-nb_workspace"
    workspace.mkdir()
    (workspace / "data").symlink_to(Path("..") / "dead-nb.data")
    (workspace / "dead-nb.py").symlink_to(Path("..") / "dead-nb.py")

    processes["dead-nb"] = NotebookProcess(
        slug="dead-nb",
        port=8500,
        process=dead_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
    )

    resp = client.post("/admin/sweep", headers={"Authorization": AUTH})
    assert resp.status_code == 200, "sweep should return 200"
    assert "dead-nb" not in state.processes, "registry should not contain reaped slug"
    assert not (tmp_path / "dead-nb.py").exists(), "source file should be removed"
    assert not data_dir.exists(), "<slug>.data/ should be rmtreed by sweep"
    assert not workspace.exists(), "<slug>_workspace/ should be rmtreed by sweep"


def test_put_notebook_returns_null_expires_at_when_ttl_indefinite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SUBPROCESS_TTL_SECONDS=0, publish reports expires_at=null (never expires)."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__SUBPROCESS_TTL_SECONDS", "0")
    client, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put(
        "/admin/notebooks/forever",
        json={"source": "x = 1"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, "valid bearer should return 200"
    body = resp.json()
    assert body["subprocess_ttl_seconds"] == 0, "response should echo the indefinite TTL"
    assert body["expires_at"] is None, (
        "expires_at should be null when age-based reaping is disabled"
    )


def test_sweep_keeps_old_alive_process_when_ttl_indefinite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ttl<=0, /admin/sweep reaps dead processes but keeps ancient live ones."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings
    from notebook_host.lifecycle import NotebookProcess

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8599")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SUBPROCESS_TTL_SECONDS", "0")
    monkeypatch.setattr(admin_mod, "kill", lambda np: None)  # no-op kill

    settings = load_settings(_env_file=None)
    processes: dict[str, Any] = {}
    state = AdminState(settings=settings, processes=processes, spawner=_make_stub_spawner()[0])
    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    # Dead process — must still be reaped even under indefinite TTL.
    dead_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    dead_proc.poll.return_value = 1
    dead_proc.pid = 11111
    (tmp_path / "dead-nb.py").write_text("x", encoding="utf-8")
    processes["dead-nb"] = NotebookProcess(
        slug="dead-nb",
        port=8500,
        process=dead_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
    )

    # Ancient but alive process — under a finite TTL this would be reaped;
    # under the indefinite TTL it must survive.
    old_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
    old_proc.poll.return_value = None
    old_proc.pid = 22222
    (tmp_path / "old-nb.py").write_text("y", encoding="utf-8")
    processes["old-nb"] = NotebookProcess(
        slug="old-nb",
        port=8501,
        process=old_proc,  # type: ignore[arg-type]
        public_host="localhost",
        host_port=8001,
        started_at=time.time() - 10_000_000,
    )

    resp = client.post("/admin/sweep", headers={"Authorization": AUTH})
    assert resp.status_code == 200, "sweep should return 200"
    reaped_slugs = [r["slug"] for r in resp.json()["reaped"]]
    assert "dead-nb" in reaped_slugs, "dead process should be reaped even under indefinite TTL"
    assert "old-nb" not in reaped_slugs, "ancient live process must survive an indefinite TTL"
    assert "old-nb" in state.processes, "old-nb should remain in the registry"


def test_lock_for_returns_same_lock_per_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AdminState.lock_for returns the same Lock instance for the same slug."""
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "x")
    settings = load_settings(_env_file=None)
    state = AdminState(settings=settings, processes={}, spawner=lambda *a, **kw: None)  # type: ignore[arg-type, return-value]

    l1 = state.lock_for("foo")
    l2 = state.lock_for("foo")
    l3 = state.lock_for("bar")
    assert l1 is l2, "same slug should return the same Lock instance"
    assert l1 is not l3, "different slugs should return different Lock instances"


# ─── E.14 bearer rotation ────────────────────────────────────────────────────


def test_admin_accepts_any_configured_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both bearers in DAIMON_NOTEBOOK__ADMIN_SECRETS are accepted."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRET", raising=False)
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRETS", "primary-token,backup-token")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")

    settings = load_settings(_env_file=None)
    stub_spawner, _ = _make_stub_spawner()

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    state = AdminState(settings=settings, processes={}, spawner=stub_spawner)
    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    # Both bearers should succeed; an unrelated one should not.
    r1 = client.put(
        "/admin/notebooks/a",
        json={"source": "x=1"},
        headers={"Authorization": "Bearer primary-token"},
    )
    assert r1.status_code == 200, "primary bearer should be accepted"

    r2 = client.put(
        "/admin/notebooks/b",
        json={"source": "x=1"},
        headers={"Authorization": "Bearer backup-token"},
    )
    assert r2.status_code == 200, "backup bearer should be accepted during rotation"

    r3 = client.put(
        "/admin/notebooks/c",
        json={"source": "x=1"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r3.status_code == 401, "non-listed bearer must still be rejected"


def test_singular_admin_secret_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_NOTEBOOK__ADMIN_SECRET (singular) is folded into admin_secrets list."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRETS", raising=False)
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "legacy-token")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")

    settings = load_settings(_env_file=None)
    assert len(settings.admin_secrets) == 1, (
        "singular ADMIN_SECRET should produce a one-element admin_secrets list"
    )

    stub_spawner, _ = _make_stub_spawner()

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    state = AdminState(settings=settings, processes={}, spawner=stub_spawner)
    app = FastAPI()
    app.include_router(create_admin_router(state))
    client = TestClient(app)

    r = client.put(
        "/admin/notebooks/a",
        json={"source": "x=1"},
        headers={"Authorization": "Bearer legacy-token"},
    )
    assert r.status_code == 200, "legacy singular alias must remain accepted"


def test_settings_raises_when_no_bearer_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ADMIN_SECRET nor ADMIN_SECRETS set → ValidationError."""
    from notebook_host.config import load_settings

    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRET", raising=False)
    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRETS", raising=False)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_admin_secrets_csv_with_whitespace_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`a , b , c` → three clean entries, whitespace stripped."""
    from notebook_host.config import load_settings

    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRET", raising=False)
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRETS", " a , b , c ")
    s = load_settings(_env_file=None)
    values = [sec.get_secret_value() for sec in s.admin_secrets]
    assert values == ["a", "b", "c"], "CSV entries should be stripped of surrounding whitespace"


# ─── pre-publish validation gate ─────────────────────────────────────────────


def test_put_notebook_rejected_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator that reports failure → 422 with cell_errors, and no spawn."""
    from notebook_host.lifecycle import ValidationResult

    def failing_validator(slug: str, file_path: Path) -> ValidationResult:
        return ValidationResult(
            ok=False,
            errors=["MultipleDefinitionError: The variable 'ax' was defined by another cell"],
        )

    client, state, stub_spawner = _make_test_app(tmp_path, monkeypatch, validator=failing_validator)
    resp = client.put(
        "/admin/notebooks/broken",
        json={"source": "import marimo"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 422, "a notebook whose cells fail validation must be rejected"
    detail = resp.json()["detail"]
    assert "MultipleDefinitionError" in str(detail["cell_errors"]), (
        "the 422 body should carry the cell errors so the client can relay them"
    )
    assert stub_spawner.call_count == 0, "a failed-validation notebook must never be spawned"
    assert "broken" not in state.processes, "no process should be registered for a rejected slug"


def test_put_notebook_failed_validation_leaves_existing_notebook_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad update to an existing slug is rejected without killing the live one."""
    from notebook_host.lifecycle import ValidationResult

    results = [ValidationResult(ok=True), ValidationResult(ok=False, errors=["boom"])]

    def validator(slug: str, file_path: Path) -> ValidationResult:
        return results.pop(0)

    client, state, stub_spawner = _make_test_app(tmp_path, monkeypatch, validator=validator)
    first = client.put(
        "/admin/notebooks/dash", json={"source": "import marimo"}, headers={"Authorization": AUTH}
    )
    assert first.status_code == 200, "the first (valid) publish should succeed"
    live = state.processes["dash"]

    second = client.put(
        "/admin/notebooks/dash",
        json={"source": "import marimo  # broken update"},
        headers={"Authorization": AUTH},
    )
    assert second.status_code == 422, "the broken update should be rejected"
    assert state.processes.get("dash") is live, (
        "the previously-published notebook must keep running when an update fails validation"
    )
    assert stub_spawner.call_count == 1, "the rejected update must not spawn a second subprocess"


def test_put_notebook_published_when_validation_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing validator → normal 200 publish, validator called with (slug, path)."""
    from notebook_host.lifecycle import ValidationResult

    seen: list[tuple[str, str]] = []

    def ok_validator(slug: str, file_path: Path) -> ValidationResult:
        seen.append((slug, file_path.name))
        return ValidationResult(ok=True)

    client, state, stub_spawner = _make_test_app(tmp_path, monkeypatch, validator=ok_validator)
    resp = client.put(
        "/admin/notebooks/good",
        json={"source": "import marimo"},
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, "a notebook that passes validation should publish normally"
    assert seen == [("good", "good.py")], "validator should be called with (slug, source path)"
    assert stub_spawner.call_count == 1, "a validated notebook should be spawned exactly once"
