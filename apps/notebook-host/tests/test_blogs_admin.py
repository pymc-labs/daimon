"""Tests for the /admin/blogs/* routes (run-mode, persistent blogs)."""

from __future__ import annotations

import subprocess
import unittest.mock
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

AUTH = "Bearer test-secret"


def _make_blog_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Any, list[tuple[str, Path, int, str]]]:
    """App with a stub spawner that records mode. Returns (client, state, calls)."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8503")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")
    settings = load_settings(_env_file=None)

    calls: list[tuple[str, Path, int, str]] = []

    def spawner(
        slug: str, file_path: Path, port: int, *, mode: str = "edit"
    ) -> subprocess.Popen[bytes]:
        calls.append((slug, file_path, port, mode))
        proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None  # alive
        proc.pid = 4321
        return proc  # type: ignore[return-value]

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    state = AdminState(settings=settings, processes={}, spawner=spawner, validator=None)
    app = FastAPI()
    app.include_router(create_admin_router(state))
    return TestClient(app, raise_server_exceptions=True), state, calls


def test_put_blog_spawns_run_mode_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.blogs_store import load_blogs

    client, _state, calls = _make_blog_app(tmp_path, monkeypatch)
    r = client.put(
        "/admin/blogs/pre-radar",
        json={"source": "import marimo as mo\napp = mo.App()"},
        headers={"Authorization": AUTH},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "pre-radar"
    assert "expires_at" not in body, "blogs are permanent — no expires_at"
    assert calls[0][3] == "run", "blog publish must spawn in run mode"
    registry = load_blogs(tmp_path / "blogs.json")
    assert "pre-radar" in registry, "a published blog must be recorded in the registry"


def test_put_blog_requires_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _state, _calls = _make_blog_app(tmp_path, monkeypatch)
    r = client.put("/admin/blogs/pre-x", json={"source": "x"})
    assert r.status_code == 401, "blog routes must require the admin bearer"


def test_list_blogs_returns_registered_with_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _state, _calls = _make_blog_app(tmp_path, monkeypatch)
    client.put(
        "/admin/blogs/pre-radar",
        json={"source": "import marimo as mo\napp = mo.App()"},
        headers={"Authorization": AUTH},
    )
    r = client.get("/admin/blogs", headers={"Authorization": AUTH})
    assert r.status_code == 200, r.text
    blogs = r.json()["blogs"]
    assert len(blogs) == 1
    assert blogs[0]["slug"] == "pre-radar"
    assert blogs[0]["alive"] is True, "a freshly spawned blog should report alive"


def test_delete_blog_unregisters_and_kills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from notebook_host.blogs_store import load_blogs

    client, state, _calls = _make_blog_app(tmp_path, monkeypatch)
    client.put(
        "/admin/blogs/pre-radar",
        json={"source": "import marimo as mo\napp = mo.App()"},
        headers={"Authorization": AUTH},
    )
    r = client.delete("/admin/blogs/pre-radar", headers={"Authorization": AUTH})
    assert r.status_code == 204, r.text
    assert "pre-radar" not in load_blogs(tmp_path / "blogs.json"), (
        "delete must drop the registry entry"
    )
    assert "pre-radar" not in state.processes, "delete must drop the tracked process"
    assert not (tmp_path / "pre-radar.py").exists(), "delete must remove the source file"
