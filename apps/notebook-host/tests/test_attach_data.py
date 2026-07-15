"""Tests for the host-side PUT /admin/notebooks/{slug}/data/{name} endpoint.

The data-PUT endpoint writes a raw blob to ``<data_dir>/<slug>.data/<name>``
atomically (tmp + os.replace) and does NOT spawn marimo. It is the host
half of the agent-side ``attach_notebook_data`` MCP tool.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import unittest.mock
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

AUTH = "Bearer test-secret"
NO_AUTH = "Bearer wrong-secret"


def _make_stub_spawner() -> unittest.mock.MagicMock:
    """Return a stub spawner that records calls and returns a fake Popen."""

    def spawner(
        slug: str, file_path: Path, port: int, *, mode: str = "edit"
    ) -> subprocess.Popen[bytes]:
        mock_proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        return mock_proc  # type: ignore[return-value]

    return unittest.mock.MagicMock(side_effect=spawner)


def _make_test_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, Any, unittest.mock.MagicMock, FastAPI]:
    """Build a FastAPI app with stub spawner. Returns (client, state, stub, app)."""
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")

    settings = load_settings(_env_file=None)
    stub_spawner = _make_stub_spawner()

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)

    processes: dict[str, Any] = {}
    state = AdminState(settings=settings, processes=processes, spawner=stub_spawner)

    app = FastAPI()
    app.include_router(create_admin_router(state))

    return TestClient(app, raise_server_exceptions=True), state, stub_spawner, app


# ─── auth + happy-path ───────────────────────────────────────────────────────


def test_put_data_without_bearer_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /admin/notebooks/{slug}/data/{name} without bearer returns 401."""
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put("/admin/notebooks/myslug/data/sales.csv", content=b"a,b\n1,2\n")
    assert resp.status_code == 401, "missing bearer should return 401"


def test_put_data_writes_blob_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT data writes raw body to <data_dir>/<slug>.data/<name> byte-for-byte."""
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    payload = b"\x00\x01\x02binary\xff\xfeblob"
    resp = client.put(
        "/admin/notebooks/myslug/data/blob.bin",
        content=payload,
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, f"valid PUT should return 200; got {resp.text}"
    body = resp.json()
    assert body["slug"] == "myslug", "response should include the slug"
    assert body["name"] == "blob.bin", "response should include the name"
    assert body["size_bytes"] == len(payload), "size_bytes should match payload length"
    assert body["path"] == "data/blob.bin", (
        "response path should be the notebook-visible 'data/<name>' form"
    )
    final_path = tmp_path / "myslug.data" / "blob.bin"
    assert final_path.read_bytes() == payload, (
        "on-disk bytes should match the request body verbatim (binary-safe)"
    )
    # No tmp file should remain on disk.
    tmp_artifacts = list((tmp_path / "myslug.data").glob(".*.tmp"))
    assert tmp_artifacts == [], f"no .tmp artifacts should remain; found {tmp_artifacts}"


def test_atomic_write_cleans_tmp_on_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write must not leave a .tmp orphan behind.

    Simulate disk-full / permission-error by monkeypatching os.replace to
    raise after tmp.write_bytes succeeds. The cleanup branch should unlink
    the tmp file before re-raising.
    """
    from notebook_host import admin as admin_module

    target_dir = tmp_path / "myslug.data"
    target_dir.mkdir()
    target = target_dir / "blob.bin"

    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        # Confirm the tmp file actually got written before we sabotage the rename.
        assert Path(str(src)).exists(), "tmp must exist before os.replace is called"
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(admin_module.os, "replace", failing_replace)

    with pytest.raises(OSError, match="No space left on device"):
        admin_module._atomic_write_bytes(target, b"x" * 10)  # pyright: ignore[reportPrivateUsage]

    assert not target.exists(), "target file should not exist after failed write"
    tmp_artifacts = list(target_dir.glob(".*.tmp"))
    assert tmp_artifacts == [], (
        f"failed write must clean its .tmp orphan; found leftover: {tmp_artifacts}"
    )
    # Sanity: restoring os.replace and retrying lets the write succeed.
    monkeypatch.setattr(admin_module.os, "replace", real_replace)
    admin_module._atomic_write_bytes(target, b"recovered")  # pyright: ignore[reportPrivateUsage]
    assert target.read_bytes() == b"recovered"


def test_put_data_does_not_spawn_marimo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The data-PUT endpoint never invokes the spawner; state.processes stays empty."""
    client, state, stub_spawner, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put(
        "/admin/notebooks/myslug/data/hello.txt",
        content=b"hi",
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 200, "PUT data should succeed"
    assert stub_spawner.call_count == 0, (
        "data PUT must not invoke the spawner; that's the source PUT's job"
    )
    assert state.processes == {}, "state.processes should remain empty after PUT data"


# ─── overwrite semantics ─────────────────────────────────────────────────────


def test_put_data_overwrites_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two PUTs with the same {slug, name} overwrite — second body wins."""
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    first = b"first body"
    second = b"second body, longer"
    r1 = client.put(
        "/admin/notebooks/over/data/file.bin",
        content=first,
        headers={"Authorization": AUTH},
    )
    assert r1.status_code == 200, "first PUT should succeed"
    r2 = client.put(
        "/admin/notebooks/over/data/file.bin",
        content=second,
        headers={"Authorization": AUTH},
    )
    assert r2.status_code == 200, "second PUT should succeed"
    final = (tmp_path / "over.data" / "file.bin").read_bytes()
    assert final == second, (
        "second body should win on overwrite; tmp+rename leaves no partial state"
    )


# ─── validation ──────────────────────────────────────────────────────────────


def test_put_data_unsafe_slug_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slug validation runs first; an unsafe slug yields 400."""
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    resp = client.put(
        "/admin/notebooks/a%00b/data/file.txt",
        content=b"x",
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 400, "slug with NUL byte should return 400"


def test_put_data_unsafe_name_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filename validation runs; a leading-dot name yields 400."""
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    # ".hidden" is rejected by safe_attachment_name (leading dot forbidden).
    resp = client.put(
        "/admin/notebooks/myslug/data/.hidden",
        content=b"x",
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 400, "leading-dot name should return 400"


# ─── size cap ────────────────────────────────────────────────────────────────


def test_put_data_oversize_returns_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Body larger than max_attachment_bytes_ceiling returns 413."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING", "1024")
    client, _, _, _ = _make_test_app(tmp_path, monkeypatch)
    big = b"y" * 2048
    resp = client.put(
        "/admin/notebooks/myslug/data/big.bin",
        content=big,
        headers={"Authorization": AUTH},
    )
    assert resp.status_code == 413, f"oversize body should return 413; got {resp.status_code}"
    assert "max_attachment_bytes_ceiling" in resp.text, "detail should reference the cap"
    assert not (tmp_path / "myslug.data" / "big.bin").exists(), (
        "no file should be written when the size cap is exceeded"
    )


# ─── concurrent PUT source + PUT data on same slug ──────────────────────────


@pytest.mark.asyncio
async def test_concurrent_put_data_and_source_serialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent PUT source + PUT data on the same slug must both succeed.

    The per-slug asyncio.Lock serializes them — without it, the source spawn
    could race with the data write under a stale cwd. Asserting both
    complete with 200 is enough to verify the lock acquired (a deadlock or
    interleaved state mutation would surface as a failure or hang).
    """
    client, _, _, app = _make_test_app(tmp_path, monkeypatch)
    del client  # use httpx.AsyncClient for true concurrency

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r_data, r_source = await asyncio.gather(
            c.put(
                "/admin/notebooks/race/data/d.csv",
                content=b"x,y\n",
                headers={"Authorization": AUTH},
            ),
            c.put(
                "/admin/notebooks/race",
                json={"source": "x = 1"},
                headers={"Authorization": AUTH},
            ),
        )
    assert r_data.status_code == 200, (
        f"data PUT should succeed under contention; got {r_data.status_code}: {r_data.text}"
    )
    assert r_source.status_code == 200, (
        f"source PUT should succeed under contention; got {r_source.status_code}: {r_source.text}"
    )
    # On-disk artifacts from both ops exist.
    assert (tmp_path / "race.data" / "d.csv").read_bytes() == b"x,y\n", (
        "data file should be readable after concurrent PUTs"
    )
    assert (tmp_path / "race.py").exists(), "source file should exist after concurrent PUTs"
