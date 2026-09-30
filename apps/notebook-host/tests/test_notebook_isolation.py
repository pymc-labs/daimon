"""Cross-notebook isolation: per-notebook access tokens and read-only scratch notebooks.

Every marimo subprocess listens on a localhost port that any other notebook's
code on the same host can reach, and the ``--base-url /n/<slug>`` in its argv
is visible to ``ps``. So neither the port nor the slug can be the access
boundary: each subprocess requires its own random token, handed to marimo on
stdin (never argv), and the URL the host returns carries it. A scratch notebook
is a read-only app unless the publisher asked for the editor.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import runpy
import subprocess
import time
import unittest.mock
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from notebook_host.jail import SlugPaths, get_slug_paths

set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]

_SECRET = "test-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(op: str, slug: str, *, jti: str = "j1") -> str:
    payload = {
        "slug": slug,
        "op": op,
        "name": None,
        "max_bytes": 1_000_000,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "jti": jti,
    }
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Any, list[dict[str, Any]]]:
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", _SECRET)
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8700")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8703")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)
    calls: list[dict[str, Any]] = []

    def spawner(slug: str, paths: SlugPaths, port: int, **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append({"slug": slug, "port": port, **kwargs})
        m: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        m.poll.return_value = None
        m.pid = 4242
        return m  # type: ignore[return-value]

    async def _fake_wait(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)
    state = AdminState(settings=settings, processes={}, spawner=spawner)
    app = FastAPI()
    app.include_router(create_admin_router(state))
    return TestClient(app, raise_server_exceptions=True), state, calls


# --- spawn: token on stdin, never argv, never --no-token ---------------------


def test_spawn_marimo_requires_a_token_passed_on_stdin_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _x: "/usr/bin/uv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    captured: dict[str, Any] = {}
    fake = unittest.mock.MagicMock(spec=subprocess.Popen)
    fake.stdin = unittest.mock.MagicMock()

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    paths = get_slug_paths(tmp_path, "nb")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# stub", encoding="utf-8")

    lifecycle.spawn_marimo("nb", paths, 8100, access_token="tok-sekret")

    cmd: list[str] = captured["cmd"]
    assert "--no-token" not in cmd, "marimo must never run without its session auth"
    assert "--token" in cmd, "session auth is switched on explicitly"
    i = cmd.index("--token-password-file")
    assert cmd[i + 1] == "-", "the token is read from stdin"
    assert not any("tok-sekret" in arg for arg in cmd), "argv is world-readable via ps"
    assert captured["stdin"] == subprocess.PIPE, "stdin is a private pipe"
    fake.stdin.write.assert_called_once_with(b"tok-sekret\n")
    fake.stdin.close.assert_called_once()


def test_notebook_url_carries_its_access_token() -> None:
    from notebook_host.lifecycle import NotebookProcess

    np = NotebookProcess(
        slug="nb",
        port=8100,
        process=unittest.mock.MagicMock(spec=subprocess.Popen),
        public_host="h",
        host_port=8001,
        public_url_base="https://nbs.example.com",
        access_token="tok",
    )
    assert np.url == "https://nbs.example.com/n/nb/?access_token=tok", (
        "the shared link is the only thing that carries the notebook's token"
    )
    assert "tok" not in repr(np), "the token stays out of reprs and logs"


# --- read-only by default ------------------------------------------------------


def test_scratch_upload_defaults_to_a_read_only_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, calls = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('notebook', 'scratch')}", content=b"# nb\n")
    assert r.status_code == 200, r.text
    assert calls[-1]["mode"] == "run", "a shared scratch link must not hand out a code editor"
    np = state.processes["scratch"]
    assert np.permanent is False, "a read-only scratch notebook is still TTL-reaped"
    assert "expires_at" in r.json(), "and still reports its expiry"
    assert "access_token=" in r.json()["url"], "the returned link carries the token"
    assert calls[-1]["access_token"] == np.access_token, "marimo gets the same token"


def test_notebook_edit_op_is_the_only_way_to_get_the_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, calls = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('notebook_edit', 'ed')}", content=b"# nb\n")
    assert r.status_code == 200, r.text
    assert calls[-1]["mode"] == "edit", "an explicit notebook_edit token spawns the editor"
    assert state.processes["ed"].permanent is False


def test_each_notebook_gets_a_distinct_token_that_survives_reupload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, _ = _make_app(tmp_path, monkeypatch)
    client.put(f"/upload/{_mint('notebook', 'a', jti='1')}", content=b"# a\n")
    client.put(f"/upload/{_mint('notebook', 'b', jti='2')}", content=b"# b\n")
    tok_a = state.processes["a"].access_token
    assert len(tok_a) >= 32, "token has real entropy"
    assert tok_a != state.processes["b"].access_token, "one notebook's token opens no other"
    client.put(f"/upload/{_mint('notebook_edit', 'a', jti='3')}", content=b"# a2\n")
    assert state.processes["a"].access_token == tok_a, "re-publishing a slug keeps its link working"


def test_ephemeral_run_mode_notebook_is_reaped() -> None:
    from notebook_host.lifecycle import NotebookProcess, should_reap

    dead = unittest.mock.MagicMock(spec=subprocess.Popen)
    dead.poll.return_value = 1
    np = NotebookProcess(
        slug="s",
        port=1,
        process=dead,
        public_host="h",
        host_port=1,
        mode="run",
        permanent=False,
    )
    assert should_reap(np, 0) is True, "a dead read-only scratch notebook is reclaimed"
    np.permanent = True
    assert should_reap(np, 0) is False, "a blog is never reaped"


def test_blog_token_is_persisted_and_reused_on_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notebook_host.main as main_mod
    from notebook_host.blogs_store import load_blogs

    client, state, calls = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('blog', 'post')}", content=b"# blog\n")
    assert r.status_code == 200, r.text
    tok = state.processes["post"].access_token
    assert f"access_token={tok}" in r.json()["url"]
    record = load_blogs(state.settings.resolved_blogs_file)["post"]
    assert record.access_token == tok, "a blog's token outlives the host process"
    assert (os.stat(state.settings.resolved_blogs_file).st_mode & 0o077) == 0, (
        "the registry holding tokens is host-only"
    )

    async def _fake_wait(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(main_mod, "wait_for_port", _fake_wait)
    state.processes.clear()
    assert asyncio.run(main_mod._spawn_blog_process(state, "post")) is True  # pyright: ignore[reportPrivateUsage]
    assert calls[-1]["access_token"] == tok, "respawn keeps the published link valid"
    assert state.processes["post"].permanent is True


def test_legacy_blog_without_token_gets_one_on_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notebook_host.main as main_mod
    from notebook_host.blogs_store import BlogRecord, load_blogs, register_blog

    _, state, calls = _make_app(tmp_path, monkeypatch)
    paths = get_slug_paths(tmp_path, "old")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# old\n", encoding="utf-8")
    register_blog(state.settings.resolved_blogs_file, BlogRecord(slug="old", created_at=1.0))

    async def _fake_wait(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(main_mod, "wait_for_port", _fake_wait)
    assert asyncio.run(main_mod._spawn_blog_process(state, "old")) is True  # pyright: ignore[reportPrivateUsage]
    tok = calls[-1]["access_token"]
    assert tok, "a pre-token blog is never served without auth"
    assert load_blogs(state.settings.resolved_blogs_file)["old"].access_token == tok


# --- real marimo: a neighbour on localhost is refused -------------------------

_NB = """import marimo

app = marimo.App()


@app.cell
def _():
    x = 1
    return


if __name__ == "__main__":
    app.run()
"""


def _readable(f: Path) -> bool:
    try:
        f.read_bytes()
    except OSError:
        return False
    return True


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["edit", "run"])
async def test_real_marimo_refuses_a_neighbour_without_the_token(tmp_path: Path, mode: str) -> None:
    from notebook_host.lifecycle import (
        NotebookProcess,
        kill,
        new_access_token,
        spawn_marimo,
        wait_for_port,
    )

    slug, port = f"iso-{mode}", 8190 if mode == "edit" else 8191
    paths = get_slug_paths(tmp_path, slug)
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text(_NB, encoding="utf-8")
    token = new_access_token()
    proc = spawn_marimo(slug, paths, port, mode=mode, access_token=token)  # type: ignore[arg-type]
    np = NotebookProcess(
        slug=slug,
        port=port,
        process=proc,
        public_host="localhost",
        host_port=8001,
        started_at=time.time(),
    )
    try:
        assert await wait_for_port(port, slug, 60.0, access_token=token) is True
        base = f"http://127.0.0.1:{port}/n/{slug}"
        async with httpx.AsyncClient(follow_redirects=False) as c:
            anon = await c.get(f"{base}/")
            assert anon.status_code != 200, "a neighbour curling the port gets no notebook"
            api = await c.post(f"{base}/api/status", json={})
            assert api.status_code == 401, "and no API"
            wrong = await c.get(f"{base}/", headers={"Authorization": "Bearer nope"})
            assert wrong.status_code != 200
            ok = await c.get(f"{base}/", headers={"Authorization": f"Bearer {token}"})
            assert ok.status_code == 200, "the holder of the token gets in"
        argvs = [f.read_bytes() for f in Path("/proc").glob("[0-9]*/cmdline") if _readable(f)]
        ours = [a for a in argvs if f"/n/{slug}".encode() in a]
        assert ours, "found the marimo process tree by its base-url"
        assert not any(token.encode() in a for a in argvs), "the token is in no process's argv"
    finally:
        kill(np)


@pytest.mark.slow
def test_real_marimo_through_the_proxy_needs_the_link_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared link works end to end (page + kernel socket); the bare slug does not."""
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings
    from notebook_host.lifecycle import kill, new_access_token, spawn_marimo, wait_for_port
    from notebook_host.proxy import create_proxy_router
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)
    slug, port = "iso-proxy", 8192
    paths = get_slug_paths(tmp_path, slug)
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text(_NB, encoding="utf-8")
    token = new_access_token()
    state = AdminState(settings=settings, processes={}, spawner=spawn_marimo)
    proc = spawn_marimo(slug, paths, port, access_token=token, mode="edit")
    np = state.make_process(slug, port, proc, access_token=token, mode="edit")
    state.processes[slug] = np
    try:
        assert asyncio.run(wait_for_port(port, slug, 60.0, access_token=token)) is True
        app = FastAPI()
        app.include_router(create_proxy_router(state))
        anon = TestClient(app)
        assert anon.get(f"/n/{slug}/", follow_redirects=False).status_code != 200, (
            "knowing the slug (it is in ps) is not enough"
        )
        with (
            pytest.raises(WebSocketDisconnect),
            anon.websocket_connect(f"/n/{slug}/ws?session_id=s-anon") as ws,
        ):
            ws.receive_text()

        viewer = TestClient(app)
        link = np.url.split(f"/n/{slug}/", 1)[1]
        first = viewer.get(f"/n/{slug}/{link}", follow_redirects=False)
        assert first.status_code in (200, 303), first.status_code
        assert viewer.get(f"/n/{slug}/").status_code == 200, "the link's cookie opens the page"
        with viewer.websocket_connect(f"/n/{slug}/ws?session_id=s-viewer") as ws:
            assert ws.receive_text(), "the kernel socket authenticates with the same cookie"
    finally:
        kill(np)
