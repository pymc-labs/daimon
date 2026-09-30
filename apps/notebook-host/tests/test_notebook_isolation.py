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
    client.put(f"/upload/{_mint('notebook', 'a', jti='3')}", content=b"# a2\n")
    assert state.processes["a"].access_token == tok_a, (
        "re-publishing a slug in the same mode keeps its link working"
    )


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


def test_switching_a_read_only_slug_to_the_editor_rotates_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, _ = _make_app(tmp_path, monkeypatch)
    client.put(f"/upload/{_mint('notebook', 'a', jti='1')}", content=b"# a\n")
    read_only = state.processes["a"].access_token
    r = client.put(f"/upload/{_mint('notebook_edit', 'a', jti='2')}", content=b"# a\n")
    assert r.status_code == 200, r.text
    assert state.processes["a"].access_token != read_only, (
        "holders of the read-only link must not become editors"
    )
    editor = state.processes["a"].access_token
    client.put(f"/upload/{_mint('blog', 'a', jti='3')}", content=b"# a\n")
    assert state.processes["a"].access_token != editor, (
        "an editor link must not keep working once the slug is a public blog"
    )


def test_editor_upload_over_a_blog_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, calls = _make_app(tmp_path, monkeypatch)
    client.put(f"/upload/{_mint('blog', 'post', jti='1')}", content=b"# blog\n")
    blog_token = state.processes["post"].access_token
    spawned = len(calls)
    r = client.put(f"/upload/{_mint('notebook_edit', 'post', jti='2')}", content=b"# x\n")
    assert r.status_code == 409, "a blog's readers must never be handed an editor"
    assert len(calls) == spawned, "nothing is respawned"
    assert state.processes["post"].access_token == blog_token


def test_marimo_is_quiet_so_its_banner_url_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """marimo prints its URL, token included, to stdout, which is the slug's log file."""
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _x: "/usr/bin/uv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    captured: dict[str, Any] = {}
    fake = unittest.mock.MagicMock(spec=subprocess.Popen)
    fake.stdin = unittest.mock.MagicMock()

    def fake_popen(cmd: list[str], **_kwargs: Any) -> object:
        captured["cmd"] = cmd
        return fake

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    paths = get_slug_paths(tmp_path, "nb")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("nb", paths, 8100, access_token="t", mode="run")
    cmd: list[str] = captured["cmd"]
    exe = len(cmd) - 1 - cmd[::-1].index("marimo")
    assert cmd[exe + 1] == "-q", "marimo -q suppresses the stdout banner"
    assert cmd[exe + 2] == "run"


def test_marimo_is_pinned_to_the_locked_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata

    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _x: "/usr/bin/uv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    captured: dict[str, Any] = {}
    fake = unittest.mock.MagicMock(spec=subprocess.Popen)
    fake.stdin = unittest.mock.MagicMock()

    def fake_popen(cmd: list[str], **_kwargs: Any) -> object:
        captured["cmd"] = cmd
        return fake

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    paths = get_slug_paths(tmp_path, "nb")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("nb", paths, 8100, access_token="t")
    cmd: list[str] = captured["cmd"]
    pinned = f"marimo=={importlib.metadata.version('marimo')}"
    assert cmd[cmd.index("--with") + 1] == pinned, "uv must not resolve a different marimo"


# --- uid reuse ------------------------------------------------------------------


def test_uid_is_not_handed_straight_to_the_next_slug(tmp_path: Path) -> None:
    from notebook_host.jail import get_or_create_slug_uid, release_slug_uid

    reg = tmp_path / "uids.json"
    a = get_or_create_slug_uid(reg, "a", start=100000, end=100009)
    get_or_create_slug_uid(reg, "b", start=100000, end=100009)
    release_slug_uid(reg, "a")
    c = get_or_create_slug_uid(reg, "c", start=100000, end=100009)
    assert c != a, "a just-released uid is quarantined, not reused first"
    assert c == 100002


def test_uid_allocation_wraps_when_the_top_of_the_range_is_reached(tmp_path: Path) -> None:
    from notebook_host.jail import get_or_create_slug_uid, release_slug_uid

    reg = tmp_path / "uids.json"
    for slug in ("a", "b", "c"):
        get_or_create_slug_uid(reg, slug, start=100000, end=100002)
    release_slug_uid(reg, "a")
    assert get_or_create_slug_uid(reg, "d", start=100000, end=100002) == 100000, (
        "the pool still recycles once every other uid is taken"
    )


def _fake_proc(root: Path, pid: int, uids: tuple[int, int, int, int]) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "status").write_text(
        f"Name:\tsleep\nPid:\t{pid}\nUid:\t{uids[0]}\t{uids[1]}\t{uids[2]}\t{uids[3]}\n"
    )


def test_kill_uid_processes_kills_every_process_owned_by_the_uid(tmp_path: Path) -> None:
    """A cell's own `Popen(..., start_new_session=True)` escapes the pgroup kill."""
    from notebook_host.jail import kill_uid_processes

    _fake_proc(tmp_path, 10, (100000, 100000, 100000, 100000))  # marimo
    _fake_proc(tmp_path, 11, (100000, 100000, 100000, 100000))  # detached survivor
    _fake_proc(tmp_path, 12, (100001, 100001, 100001, 100001))  # another notebook
    _fake_proc(tmp_path, 13, (0, 100000, 0, 0))  # setuid-ish: any id matching counts
    (tmp_path / "self").mkdir()
    killed: list[int] = []

    def fake_kill(pid: int, _sig: int) -> None:
        killed.append(pid)
        import shutil

        shutil.rmtree(tmp_path / str(pid))

    kill_uid_processes(100000, proc_root=tmp_path, kill=fake_kill)
    assert sorted(killed) == [10, 11, 13], "every process of the uid, and nothing else"


def test_releasing_a_slug_kills_its_uid_before_the_uid_is_freed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host import jail

    reg = tmp_path / "uids.json"
    uid = jail.get_or_create_slug_uid(reg, "a", start=100000, end=100009)
    order: list[str] = []

    def fake_kill_uid(u: int, **_kw: object) -> None:
        assert u == uid
        assert jail.load_uid_registry(reg).get("a") == uid, "still reserved while killing"
        order.append("killed")

    monkeypatch.setattr(jail, "kill_uid_processes", fake_kill_uid)
    jail.remove_slug_tree(tmp_path, "a", uids_file=reg)
    assert order == ["killed"], "a survivor must die before its uid can be handed out"
    assert "a" not in jail.load_uid_registry(reg)


# --- access log ----------------------------------------------------------------


def test_uvicorn_access_log_redacts_the_access_token() -> None:
    import io
    import logging
    import logging.config

    from notebook_host.__main__ import uvicorn_log_config

    config = uvicorn_log_config()
    logging.config.dictConfig(config)
    buf = io.StringIO()
    handler = logging.getLogger("uvicorn.access").handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buf)  # pyright: ignore[reportUnknownMemberType]
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:1",
        "GET",
        "/n/nb/?access_token=SEKRET-tok&x=1",
        "1.1",
        303,
    )
    out = buf.getvalue()
    assert "SEKRET-tok" not in out, "the link's token must not land in the host log"
    assert "access_token=[redacted]&x=1" in out


# --- cross-notebook requests from the same origin ---------------------------------


def _proxy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings
    from notebook_host.lifecycle import NotebookProcess
    from notebook_host.proxy import create_proxy_router

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    settings = load_settings(_env_file=None)
    proc = unittest.mock.MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    state = AdminState(settings=settings, processes={}, spawner=unittest.mock.MagicMock())
    for slug, port in (("a", 9001), ("b", 9002)):
        state.processes[slug] = NotebookProcess(
            slug=slug, port=port, process=proc, public_host="h", host_port=1, access_token="t"
        )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    real_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("notebook_host.proxy.httpx.AsyncClient", fake_client)
    app = FastAPI()
    app.include_router(create_proxy_router(state))
    return TestClient(app), seen


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/a/"},
        {"sec-fetch-site": "same-origin"},  # referrerPolicy: 'no-referrer'
        {
            "sec-fetch-site": "same-origin",
            "sec-fetch-dest": "iframe",
            "referer": "https://nbs.example.com/n/a/x",
        },
        {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/b-evil/"},
    ],
)
def test_proxy_refuses_one_notebook_page_reaching_into_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    client, seen = _proxy_client(tmp_path, monkeypatch)
    r = client.get("/n/b/api/status", headers=headers)
    assert r.status_code == 403, "notebook a's JS must not ride b's cookie"
    assert seen == [], "nothing reaches b's marimo"


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/b/"},
        {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/b"},
        {"sec-fetch-site": "cross-site", "referer": "https://discord.com/"},
        {"sec-fetch-site": "none"},
        {},
    ],
)
def test_proxy_allows_a_notebooks_own_page_and_opening_its_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    client, seen = _proxy_client(tmp_path, monkeypatch)
    r = client.get("/n/b/api/status", headers=headers)
    assert r.status_code == 200, r.text
    assert len(seen) == 1


def test_proxy_refuses_a_socket_opened_from_another_notebooks_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client, _ = _proxy_client(tmp_path, monkeypatch)
    headers = {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/a/"}
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/n/b/ws?session_id=x", headers=headers) as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 1008


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
