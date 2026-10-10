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
import logging
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


def _mint_data(slug: str, name: str) -> str:
    payload: dict[str, object] = {
        "slug": slug,
        "op": "data",
        "name": name,
        "max_bytes": 1_000_000,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "jti": f"data-{name}",
    }
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _mint(op: str, slug: str, *, jti: str = "j1", tenant: str | None = None) -> str:
    payload: dict[str, object] = {
        "slug": slug,
        "op": op,
        "name": None,
        "max_bytes": 1_000_000,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "jti": jti,
    }
    if tenant is not None:
        payload["tenant"] = tenant
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, allow_editable: bool = False
) -> tuple[TestClient, Any, list[dict[str, Any]]]:
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", _SECRET)
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8700")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8703")
    set_unjailed_test_env(monkeypatch)
    if allow_editable:
        monkeypatch.setenv("DAIMON_NOTEBOOK__ALLOW_EDITABLE", "true")
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


def test_notebook_url_keeps_its_access_token_on_the_host() -> None:
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
    assert np.url.startswith("https://nbs.example.com/s/nb/")
    assert "tok" not in np.url
    assert "access_token" not in np.url
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
    assert np.registered is True, "a read-only scratch notebook starts on a visit once stopped"
    assert r.json()["expires_at"] is not None, "and still reports its expiry"
    assert r.json()["permanent"] is False
    assert "access_token=" not in r.json()["url"], "the returned link keeps the token on the host"
    assert calls[-1]["access_token"] == np.access_token, "marimo gets the same token"


def test_notebook_edit_op_is_the_only_way_to_get_the_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, calls = _make_app(tmp_path, monkeypatch, allow_editable=True)
    r = client.put(f"/upload/{_mint('notebook_edit', 'ed')}", content=b"# nb\n")
    assert r.status_code == 200, r.text
    assert calls[-1]["mode"] == "edit", "an explicit notebook_edit token spawns the editor"
    assert state.processes["ed"].registered is False, "the editor is never restarted on a visit"


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


def test_registered_notebook_is_never_reaped_by_the_editor_ttl() -> None:
    from notebook_host.lifecycle import NotebookProcess, should_reap

    dead = unittest.mock.MagicMock(spec=subprocess.Popen)
    dead.poll.return_value = 1
    np = NotebookProcess(
        slug="s",
        port=1,
        process=dead,
        public_host="h",
        host_port=1,
        mode="edit",
        registered=False,
    )
    assert should_reap(np, 0) is True, "a dead editor is reclaimed"
    np.registered = True
    assert should_reap(np, 0) is False, "a registered notebook's files outlive its process"


def test_blog_token_is_persisted_and_reused_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notebook_host.lazy_spawn as lazy_mod
    from notebook_host.blogs_store import load_blogs
    from notebook_host.lazy_spawn import ensure_running

    client, state, calls = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('blog', 'post')}", content=b"# blog\n")
    assert r.status_code == 200, r.text
    tok = state.processes["post"].access_token
    assert tok not in r.json()["url"]
    assert "/s/" in r.json()["url"]
    record = load_blogs(state.settings.resolved_blogs_file)["post"]
    assert record.access_token == tok, "a blog's token outlives the host process"
    assert (os.stat(state.settings.resolved_blogs_file).st_mode & 0o077) == 0, (
        "the registry holding tokens is host-only"
    )

    async def _fake_wait(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(lazy_mod, "wait_for_port", _fake_wait)
    state.processes.clear()
    assert asyncio.run(ensure_running(state, "post", now=time.time())) is not None
    assert calls[-1]["access_token"] == tok, "a restart keeps the published link valid"
    assert state.processes["post"].registered is True


def test_switching_a_read_only_slug_to_the_editor_rotates_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state, _ = _make_app(tmp_path, monkeypatch, allow_editable=True)
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
    client, state, calls = _make_app(tmp_path, monkeypatch, allow_editable=True)
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


def test_kill_uid_processes_signals_the_whole_uid_and_verifies_none_are_left(
    tmp_path: Path,
) -> None:
    """A cell's own `Popen(..., start_new_session=True)` escapes the pgroup kill."""
    from notebook_host.jail import kill_uid_processes

    _fake_proc(tmp_path, 10, (100000, 100000, 100000, 100000))  # marimo
    _fake_proc(tmp_path, 11, (100000, 100000, 100000, 100000))  # detached survivor
    _fake_proc(tmp_path, 12, (100001, 100001, 100001, 100001))  # another notebook
    (tmp_path / "self").mkdir()
    signalled: list[int] = []

    def fake_signal_all_as(uid: int) -> None:
        # setuid(uid) + kill(-1, SIGKILL): the kernel kills every process of the uid.
        signalled.append(uid)
        import shutil

        for pid in (10, 11):
            shutil.rmtree(tmp_path / str(pid), ignore_errors=True)

    kill_uid_processes(100000, proc_root=tmp_path, signal_all_as=fake_signal_all_as)
    assert signalled == [100000], "one kernel-side kill of the whole uid"
    assert (tmp_path / "12").exists(), "another notebook's process is untouched"


def test_kill_uid_processes_fails_closed_when_something_survives(tmp_path: Path) -> None:
    from notebook_host.jail import UidStillInUseError, kill_uid_processes

    _fake_proc(tmp_path, 10, (100000, 100000, 100000, 100000))
    with pytest.raises(UidStillInUseError):
        kill_uid_processes(
            100000, proc_root=tmp_path, signal_all_as=lambda _uid: None, deadline_s=0.2
        )


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
    monkeypatch.setattr(jail, "remove_uid_files", lambda _u, **_kw: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    jail.remove_slug_tree(tmp_path, "a", uids_file=reg)
    assert order == ["killed"], "a survivor must die before its uid can be handed out"
    assert "a" not in jail.load_uid_registry(reg)


def test_a_uid_with_a_survivor_is_quarantined_not_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host import jail

    reg = tmp_path / "uids.json"
    uid = jail.get_or_create_slug_uid(reg, "a", start=100000, end=100001)

    def stuck(_u: int, **_kw: object) -> None:
        raise jail.UidStillInUseError("survivor")

    monkeypatch.setattr(jail, "kill_uid_processes", stuck)
    jail.remove_slug_tree(tmp_path, "a", uids_file=reg)
    registry = jail.load_uid_registry(reg)
    assert "a" not in registry, "the slug is gone"
    assert uid in registry.values(), "but its uid stays reserved"
    for slug in ("b",):
        assert jail.get_or_create_slug_uid(reg, slug, start=100000, end=100001) != uid
    with pytest.raises(jail.UidPoolExhaustedError):
        jail.get_or_create_slug_uid(reg, "c", start=100000, end=100001)


def test_releasing_a_uid_deletes_its_files_in_shared_temp_dirs(tmp_path: Path) -> None:
    from notebook_host.jail import remove_uid_files

    me = os.getuid()
    shm = tmp_path / "shm"
    shm.mkdir()
    (shm / "planted").write_text("x")
    nested = shm / "dir"
    nested.mkdir()
    (nested / "f").write_text("y")
    (shm / "link").symlink_to("/etc/passwd")
    remove_uid_files(me, roots=(shm,))
    assert list(shm.iterdir()) == [], "everything the uid owned in /tmp and /dev/shm is gone"
    assert Path("/etc/passwd").exists(), "symlinks are unlinked, never followed"
    other = shm / "other"
    other.write_text("z")
    remove_uid_files(me + 1, roots=(shm,))
    assert other.exists(), "another uid's files are left alone"


def test_jailed_preexec_sets_no_new_privs_and_a_process_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resource

    from notebook_host import jail

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(jail.os, "setgroups", lambda g: calls.append(("setgroups", g)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(jail.os, "setgid", lambda g: calls.append(("setgid", g)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(jail.os, "setuid", lambda u: calls.append(("setuid", u)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(jail, "_set_no_new_privs", lambda: calls.append(("no_new_privs", None)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda which, lim: calls.append(("rlimit", (which, lim))),  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    jail.build_jailed_preexec(100000, rlimit_as_bytes=None, rlimit_cpu_seconds=None)()
    names = [c[0] for c in calls]
    assert "no_new_privs" in names, "setuid binaries can't hand the notebook root back"
    assert names.index("no_new_privs") < names.index("setuid")
    nproc = [c[1] for c in calls if c[0] == "rlimit" and c[1][0] == resource.RLIMIT_NPROC]  # type: ignore[index]
    assert nproc, "the uid's process count is capped so a fork bomb can't outrun the kill"


def test_jailed_marimo_gets_a_private_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _x: "/usr/bin/uv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    captured: dict[str, Any] = {}
    fake = unittest.mock.MagicMock(spec=subprocess.Popen)
    fake.stdin = unittest.mock.MagicMock()

    def fake_popen(_cmd: list[str], **kwargs: Any) -> object:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    paths = get_slug_paths(tmp_path, "nb")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("nb", paths, 8100, access_token="t")
    assert captured["env"]["TMPDIR"] == str(paths.tmp), "temp files stay in the slug's tree"
    assert paths.tmp.is_dir()


def test_switching_editor_to_read_only_wipes_what_the_editor_could_plant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _make_app(tmp_path, monkeypatch, allow_editable=True)
    client.put(f"/upload/{_mint('notebook_edit', 'a', jti='1')}", content=b"# a\n")
    paths = get_slug_paths(tmp_path, "a")
    for d in (paths.home, paths.workspace, paths.tmp):
        d.mkdir(parents=True, exist_ok=True)
        (d / "planted.py").write_text("evil")
    (paths.data / "keep.csv").write_text("1")
    r = client.put(f"/upload/{_mint('notebook', 'a', jti='2')}", content=b"# a\n")
    assert r.status_code == 200, r.text
    for d in (paths.home, paths.workspace, paths.tmp):
        assert not (d / "planted.py").exists(), f"{d.name} is reset when the editor goes away"
    assert (paths.data / "keep.csv").exists(), "attachments are content, and are kept"


def test_editor_uploads_need_the_host_switch_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, calls = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('notebook_edit', 'a')}", content=b"# a\n")
    assert r.status_code == 403, "a leaked or forged editor capability is refused by default"
    assert calls == []


def test_admin_put_notebook_is_read_only_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, calls = _make_app(tmp_path, monkeypatch)
    auth = {"Authorization": f"Bearer {_SECRET}"}
    r = client.put("/admin/notebooks/a", json={"source": "# a\n"}, headers=auth)
    assert r.status_code == 200, r.text
    assert calls[-1]["mode"] == "run"
    r = client.put("/admin/notebooks/b", json={"source": "# b\n", "editable": True}, headers=auth)
    assert r.status_code == 403, "the editor needs allow_editable on the host"


def test_restarting_a_blog_kills_leftovers_of_its_uid_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notebook_host.lazy_spawn as lazy_mod
    from notebook_host.blogs_store import BlogRecord, register_blog

    _, state, calls = _make_app(tmp_path, monkeypatch)
    paths = get_slug_paths(tmp_path, "post")
    paths.notebook.parent.mkdir(parents=True, exist_ok=True)
    paths.notebook.write_text("# post\n", encoding="utf-8")
    register_blog(state.settings.resolved_blogs_file, BlogRecord(slug="post", created_at=1.0))
    monkeypatch.setattr(lazy_mod, "resolve_jail_uid", lambda *_a, **_k: 100007)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(lazy_mod, "ensure_slug_jail", lambda d, s, uid=None: get_slug_paths(d, s))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    killed: list[int] = []
    monkeypatch.setattr(lazy_mod, "kill_uid_processes", lambda uid, **_k: killed.append(uid))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    async def _fake_wait(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(lazy_mod, "wait_for_port", _fake_wait)
    assert asyncio.run(lazy_mod.ensure_running(state, "post", now=time.time())) is not None
    assert killed == [100007], "a stopped blog's detached children don't share the new process"
    assert calls


def test_host_refuses_to_boot_serving_tokenized_links_over_plain_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notebook_host.config import load_settings
    from notebook_host.main import check_link_security

    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_HOST", "nbs.example.com")
    with pytest.raises(RuntimeError, match="PUBLIC_URL_BASE"):
        check_link_security(load_settings(_env_file=None))
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_URL_BASE", "http://nbs.example.com")
    with pytest.raises(RuntimeError, match="https"):
        check_link_security(load_settings(_env_file=None))
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_URL_BASE", "https://nbs.example.com")
    check_link_security(load_settings(_env_file=None))
    monkeypatch.delenv("DAIMON_NOTEBOOK__PUBLIC_URL_BASE")
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_HOST", "localhost")
    check_link_security(load_settings(_env_file=None))
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_HOST", "nbs.internal")
    monkeypatch.setenv("DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS", "true")
    check_link_security(load_settings(_env_file=None))


def test_notebook_host_pins_marimo_to_the_locked_version() -> None:
    """The Docker image installs from pyproject.toml alone, without uv.lock."""
    import importlib.metadata
    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps: list[str] = pyproject["project"]["dependencies"]
    marimo = [d for d in deps if d.replace(" ", "").startswith("marimo")]
    assert marimo == [f"marimo=={importlib.metadata.version('marimo')}"], (
        "an unpinned range lets the image resolve a marimo nobody tested"
    )


# --- access log ----------------------------------------------------------------


def _root_output(config: dict[str, Any]) -> str:
    import logging

    stream = logging.getLogger().handlers[0]
    assert isinstance(stream, logging.StreamHandler)
    value = getattr(stream.stream, "getvalue", None)
    return value() if callable(value) else ""  # pyright: ignore[reportUnknownVariableType]


def test_host_logs_redact_the_access_token_in_every_form() -> None:
    import io
    import logging
    import logging.config

    from notebook_host.__main__ import uvicorn_log_config

    logging.config.dictConfig(uvicorn_log_config())
    buf = io.StringIO()
    handlers = [*logging.getLogger("uvicorn.access").handlers, *logging.getLogger().handlers]
    assert handlers, "uvicorn's handlers are configured, the root logger's too"
    for handler in handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(buf)  # pyright: ignore[reportUnknownMemberType]
    access = logging.getLogger("uvicorn.access")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:1",
        "GET",
        "/n/nb/?access_token=SEKRET-tok&x=1",
        "1.1",
        303,
    )
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:1",
        "GET",
        "/n/nb/auth/login?next=%2Fn%2Fnb%2F%3Faccess_token%3DENC-tok%26x%3D1",
        "1.1",
        303,
    )
    logging.getLogger("notebook_host.proxy").warning("saw /n/nb/?ACCESS_TOKEN=APP-tok")
    out = buf.getvalue()
    assert "SEKRET-tok" not in out, "the link's token must not land in the host log"
    assert "access_token=[redacted]&x=1" in out
    assert "ENC-tok" not in out, "nor its url-encoded form in marimo's next="
    assert "APP-tok" not in out, "nor anything the app's own loggers print"


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
        {"sec-fetch-site": "same-origin"},  # browser reload: no Referer
        {"sec-fetch-site": "same-origin", "referer": "https://nbs.example.com/n/a/"},  # a link
        {"sec-fetch-site": "cross-site", "referer": "https://discord.com/"},
        {"sec-fetch-site": "none"},
        {},
    ],
)
def test_proxy_does_not_pretend_to_isolate_notebooks_in_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """All notebooks share one origin; header checks there are bypassable theatre.

    Page JS can set any same-origin ``referrer``, so a Referer gate stops no
    attacker and only breaks reloads and links. One host = one client instead.
    """
    client, seen = _proxy_client(tmp_path, monkeypatch)
    r = client.get("/n/b/api/status", headers=headers)
    assert r.status_code == 200, r.text
    assert len(seen) == 1


# --- per-notebook origins (origin_base) -------------------------------------------


def _origin_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Any, list[httpx.Request], dict[str, str]]:
    from notebook_host.admin import AdminState
    from notebook_host.config import load_settings
    from notebook_host.proxy import create_proxy_router

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ORIGIN_BASE", "nb.example.com")
    settings = load_settings(_env_file=None)
    proc = unittest.mock.MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    state = AdminState(settings=settings, processes={}, spawner=unittest.mock.MagicMock())
    hosts: dict[str, str] = {}
    for slug, port, tok in (("a", 9001, "tok-a"), ("b", 9002, "tok-b")):
        np = state.make_process(slug, port, proc, access_token=tok, mode="edit")
        state.processes[slug] = np
        hosts[slug] = f"{np.origin_label}.nb.example.com"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text="ok",
            headers=[
                ("set-cookie", "session_1=x; Domain=nb.example.com; Path=/n/b; HttpOnly"),
                ("set-cookie", "other=y; Path=/"),
            ],
        )

    real_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("notebook_host.proxy.httpx.AsyncClient", fake_client)
    app = FastAPI()
    app.include_router(create_proxy_router(state))
    return TestClient(app), state, seen, hosts


def test_each_notebook_gets_its_own_unguessable_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state, _, hosts = _origin_client(tmp_path, monkeypatch)
    url = state.processes["b"].url
    assert url.startswith(f"https://{hosts['b']}/s/b/"), url
    label = hosts["b"].split(".", 1)[0]
    assert len(label) == 32 and label != "b", "the label is random-looking, never the slug"
    assert hosts["a"] != hosts["b"], "two notebooks never share an origin"


def test_origin_mode_routes_by_exact_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, seen, hosts = _origin_client(tmp_path, monkeypatch)
    assert client.get("/n/b/", headers={"host": hosts["b"]}).status_code == 200
    assert client.get("/n/b/", headers={"host": "nb.example.com"}).status_code == 404, (
        "path mode on the shared host is refused"
    )
    assert client.get("/n/b/", headers={"host": hosts["a"]}).status_code == 404, (
        "b's path on a's origin reaches nothing"
    )
    assert client.get("/n/b/", headers={"host": "evil." + hosts["b"]}).status_code == 404
    assert len(seen) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://A"},  # replaced below with a's real origin
        {"sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"},
        {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"},
        {"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"},
        {"origin": "null"},
    ],
)
def test_origin_mode_refuses_requests_from_another_notebooks_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    client, _, seen, hosts = _origin_client(tmp_path, monkeypatch)
    if headers.get("origin") == "https://A":
        headers = {"origin": f"https://{hosts['a']}"}
    r = client.post("/n/b/api/kernel/run", headers={"host": hosts["b"], **headers})
    assert r.status_code == 403, "the browser-set Origin/Sec-Fetch headers give a's page away"
    assert seen == []


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, old browsers
        {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"},
        {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
        {
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        },
        {"origin": "ORIGIN_B"},
    ],
)
def test_origin_mode_serves_its_own_page_and_the_link_opened_from_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    client, _, seen, hosts = _origin_client(tmp_path, monkeypatch)
    if headers.get("origin") == "ORIGIN_B":
        headers = {"origin": f"https://{hosts['b']}"}
    r = client.get("/n/b/", headers={"host": hosts["b"], **headers})
    assert r.status_code == 200, r.text
    assert len(seen) == 1


def test_origin_mode_cookies_are_host_only_and_framing_is_self_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, hosts = _origin_client(tmp_path, monkeypatch)
    r = client.get("/n/b/", headers={"host": hosts["b"]})
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2, "every Set-Cookie is relayed"
    assert all("domain" not in c.lower() for c in cookies), "no cookie is shared with siblings"
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_origin_mode_websocket_needs_the_exact_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client, _, _, hosts = _origin_client(tmp_path, monkeypatch)
    for origin in (f"https://{hosts['a']}", None, f"http://{hosts['b']}", "https://nb.example.com"):
        headers = {"host": hosts["b"]}
        if origin is not None:
            headers["origin"] = origin
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/n/b/ws?session_id=x", headers=headers) as ws,
        ):
            ws.receive_text()
        assert exc.value.code == 1008, f"origin {origin!r} must not open b's kernel socket"


_T1 = "11111111-1111-4111-8111-111111111111"
_T2 = "22222222-2222-4222-8222-222222222222"
_T3 = "33333333-3333-4333-8333-333333333333"


def test_shared_origin_public_host_serves_listed_tenants_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listed tenants share the origin; anyone else gets a 403 naming the setting and id."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_URL_BASE", "https://nbs.example.com")
    # Upper case: the setting compares canonical UUIDs, not strings.
    monkeypatch.setenv("DAIMON_NOTEBOOK__TENANTS", f'["{_T1}", "{_T2.upper()}"]')
    client, _, calls = _make_app(tmp_path, monkeypatch)
    for i, tenant in enumerate((_T1, _T2)):
        r = client.put(
            f"/upload/{_mint('notebook', f's{i}', jti=str(i), tenant=tenant)}", content=b"#\n"
        )
        assert r.status_code == 200, f"listed tenant {tenant} must be served: {r.text}"
    other = client.put(f"/upload/{_mint('notebook', 'c', jti='3', tenant=_T3)}", content=b"#\n")
    assert other.status_code == 403, "an unlisted tenant can't share the origin"
    detail = other.json()["detail"]
    assert _T3 in detail and "DAIMON_NOTEBOOK__TENANTS" in detail, (
        f"the refusal must carry the id and the setting for the operator to copy: {detail}"
    )
    anon = client.put(f"/upload/{_mint('notebook', 'd', jti='4')}", content=b"#\n")
    assert anon.status_code == 403, "a token naming no tenant fails closed"
    assert "DAIMON_NOTEBOOK__TENANTS" in anon.json()["detail"], "names the setting"
    assert len(calls) == 2, "only the two listed tenants' uploads spawn a notebook"


def test_shared_origin_public_host_with_no_tenants_refuses_every_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list admits nobody, and a stale first-come claim file is not honoured."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__PUBLIC_URL_BASE", "https://nbs.example.com")
    client, _, calls = _make_app(tmp_path, monkeypatch)
    (tmp_path / "tenant.json").write_text(json.dumps({"tenant": _T1}))
    r = client.put(f"/upload/{_mint('notebook', 'a', tenant=_T1)}", content=b"#\n")
    assert r.status_code == 403, "tenant.json from the old first-come claim must be ignored"
    detail = r.json()["detail"]
    assert _T1 in detail and "DAIMON_NOTEBOOK__TENANTS" in detail, detail
    assert calls == [], "a refused upload never spawns"


def test_shared_origin_boot_warns_when_no_tenant_is_listed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from notebook_host.config import Settings
    from notebook_host.main import warn_shared_origin

    settings = Settings(admin_secrets=["x"], public_url_base="https://nbs.example.com")  # pyright: ignore[reportCallIssue, reportArgumentType]
    with caplog.at_level(logging.WARNING, logger="notebook_host.main"):
        warn_shared_origin(settings)
    assert "DAIMON_NOTEBOOK__TENANTS is empty" in caplog.text, caplog.text


def test_local_dev_host_admits_any_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, calls = _make_app(tmp_path, monkeypatch)
    for i, tenant in enumerate((_T3, None)):
        r = client.put(
            f"/upload/{_mint('notebook', f's{i}', jti=str(i), tenant=tenant)}", content=b"#\n"
        )
        assert r.status_code == 200, f"localhost needs no tenant list: {r.text}"
    assert len(calls) == 2


def test_per_origin_host_serves_many_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_NOTEBOOK__ORIGIN_BASE", "nb.example.com")
    # Per-notebook origins isolate every tenant, so the shared-origin list is moot.
    monkeypatch.setenv("DAIMON_NOTEBOOK__TENANTS", f'["{_T3}"]')
    client, state, _ = _make_app(tmp_path, monkeypatch)
    for i, tenant in enumerate((_T1, _T2)):
        r = client.put(
            f"/upload/{_mint('notebook', f's{i}', jti=str(i), tenant=tenant)}", content=b"#\n"
        )
        assert r.status_code == 200, r.text
    assert state.processes["s0"].url.startswith(f"https://{state.processes['s0'].origin_label}.")


def test_origin_mode_boot_check_wants_https() -> None:
    from notebook_host.config import Settings
    from notebook_host.main import check_link_security

    base = {"admin_secrets": ["x"], "origin_base": "nb.example.com"}
    check_link_security(Settings(**base))  # pyright: ignore[reportArgumentType]
    with pytest.raises(RuntimeError, match="ORIGIN_SCHEME"):
        check_link_security(Settings(**base, origin_scheme="http"))  # pyright: ignore[reportArgumentType]
    check_link_security(
        Settings(admin_secrets=["x"], origin_base="localhost:8001", origin_scheme="http")
    )  # pyright: ignore[reportArgumentType]


# --- log redaction through a real httpx request -----------------------------------


def test_httpx_request_logs_never_carry_the_token() -> None:
    """httpx logs its URL as an ``httpx.URL`` arg, which arg-wise redaction skipped."""
    import io
    import logging
    import logging.config

    from notebook_host.__main__ import uvicorn_log_config

    logging.config.dictConfig(uvicorn_log_config())
    buf = io.StringIO()
    for handler in logging.getLogger().handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(buf)  # pyright: ignore[reportUnknownMemberType]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200))
    with httpx.Client(transport=transport) as c:
        c.get("http://127.0.0.1:1/n/nb/?access_token=HTTPX-tok")
    # Even if httpx is turned back up to INFO, a non-str arg is redacted.
    logging.getLogger("httpx").setLevel(logging.INFO)
    with httpx.Client(transport=transport) as c:
        c.get("http://127.0.0.1:1/n/nb/?access_token=HTTPX-tok2")
    logging.getLogger("notebook_host").warning(
        "url %s", httpx.URL("http://h/n/nb/?access_token=URL-tok")
    )
    out = buf.getvalue()
    assert "HTTPX-tok" not in out and "URL-tok" not in out, out
    assert "access_token=[redacted]" in out, "the records are still logged, redacted"


# --- root must never follow a symlink the jail uid planted --------------------------


def test_ensure_slug_jail_never_chowns_or_chmods_through_a_planted_symlink(
    tmp_path: Path,
) -> None:
    """A cell renames home away and plants home -> /etc; the next spawn must not chown /etc."""
    from notebook_host.jail import ensure_slug_jail

    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    (victim / "passwd").write_text("root:x:0:0")
    data_dir = tmp_path / "nbs"
    paths = ensure_slug_jail(data_dir, "s")
    # Legacy tree: the slug root was owned by the jail uid, so it could swap.
    paths.root.chmod(0o700)
    paths.home.rename(paths.root / "h2")
    paths.home.symlink_to(victim)
    paths.log.symlink_to(victim / "passwd")
    paths.notebook.symlink_to(victim / "passwd")

    ensure_slug_jail(data_dir, "s")

    assert victim.stat().st_mode & 0o777 == 0o755, "the symlink target is untouched"
    assert (victim / "passwd").read_text() == "root:x:0:0"
    assert paths.home.is_dir() and not paths.home.is_symlink(), "home is a real dir again"
    assert not paths.log.is_symlink() and not paths.notebook.is_symlink()
    assert paths.root.stat().st_mode & 0o777 == 0o711
    assert paths.root.stat().st_uid == os.geteuid(), "the slug root belongs to the host"


def test_host_file_writes_never_follow_a_planted_symlink(tmp_path: Path) -> None:
    from notebook_host.admin import _atomic_write_bytes  # pyright: ignore[reportPrivateUsage]
    from notebook_host.jail import ensure_slug_jail

    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    paths = ensure_slug_jail(tmp_path / "nbs", "s")
    # The data dir belongs to the jail uid: it can plant both names.
    (paths.data / ".x.csv.tmp").symlink_to(victim)
    (paths.data / "y.csv").symlink_to(victim)
    _atomic_write_bytes(paths.data / "x.csv", b"attached")
    _atomic_write_bytes(paths.data / "y.csv", b"attached")
    assert victim.read_text() == "original", "root wrote through no link"
    assert (paths.data / "x.csv").read_bytes() == b"attached"
    assert not (paths.data / "y.csv").is_symlink()
    assert (paths.data / "x.csv").stat().st_mode & 0o777 == 0o600


def test_marimo_log_is_opened_without_following_a_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from notebook_host import lifecycle
    from notebook_host.jail import ensure_slug_jail

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _x: "/usr/bin/uv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    fake = unittest.mock.MagicMock(spec=subprocess.Popen)
    fake.stdin = unittest.mock.MagicMock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *_a, **_k: fake)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    paths = ensure_slug_jail(tmp_path / "nbs", "s")
    paths.notebook.write_text("# nb")
    paths.log.symlink_to(victim)
    with pytest.raises(OSError):
        lifecycle.spawn_marimo("s", paths, 8100, access_token="t")
    assert victim.read_text() == "original", "the host never appends through a planted link"


def test_mode_wipe_removes_a_symlinked_home_instead_of_leaving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _make_app(tmp_path, monkeypatch, allow_editable=True)
    client.put(f"/upload/{_mint('notebook_edit', 'a', jti='1')}", content=b"# a\n")
    paths = get_slug_paths(tmp_path, "a")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    paths.root.chmod(0o700)  # a legacy, uid-owned root
    import shutil

    shutil.rmtree(paths.home)
    paths.home.symlink_to(victim)
    r = client.put(f"/upload/{_mint('notebook', 'a', jti='2')}", content=b"# a\n")
    assert r.status_code == 200, r.text
    assert (victim / "keep").exists(), "the wipe never recursed through the link"
    assert paths.home.is_dir() and not paths.home.is_symlink()


def test_legacy_world_readable_files_are_locked_down_and_hard_links_dropped(
    tmp_path: Path,
) -> None:
    """Older releases left 0644 files under a 0700 root; the root is 0711 now."""
    from notebook_host.jail import ensure_slug_jail

    data_dir = tmp_path / "nbs"
    paths = ensure_slug_jail(data_dir, "s")
    paths.root.chmod(0o700)
    paths.notebook.write_text("# blog source")
    paths.notebook.chmod(0o644)
    registry = data_dir / "uids.json"
    registry.write_text('{"s": 100000}')
    registry.chmod(0o600)
    os.link(registry, paths.log)  # a legacy hard link from the log to a registry

    ensure_slug_jail(data_dir, "s")

    assert paths.notebook.stat().st_mode & 0o777 == 0o600, "no other uid reads the source"
    assert paths.notebook.read_text() == "# blog source"
    assert not paths.log.exists() or not os.path.samefile(paths.log, registry), (
        "the hard link is gone, not chmodded or appended to"
    )
    assert registry.stat().st_nlink == 1 and registry.read_text() == '{"s": 100000}'


def test_legacy_world_readable_log_is_made_host_only(tmp_path: Path) -> None:
    from notebook_host.jail import ensure_slug_jail, open_log_nofollow

    paths = ensure_slug_jail(tmp_path / "nbs", "s")
    paths.log.write_text("old log")
    paths.log.chmod(0o644)
    ensure_slug_jail(tmp_path / "nbs", "s")
    assert paths.log.stat().st_mode & 0o777 == 0o600
    paths.log.chmod(0o644)
    os.close(open_log_nofollow(paths.log))
    assert paths.log.stat().st_mode & 0o777 == 0o600, "the fd is fchmodded, not only on create"
    other = tmp_path / "other"
    other.write_text("x")
    paths.log.unlink()
    os.link(other, paths.log)
    with pytest.raises(OSError):
        open_log_nofollow(paths.log)


def test_remove_uid_files_walks_by_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.walk checks a path and then descends it by name: another uid can swap it."""
    from notebook_host import jail

    def _no_walk(*_a: object, **_k: object) -> object:
        raise AssertionError("os.walk is steerable; walk by fd")

    monkeypatch.setattr(jail.os, "walk", _no_walk)
    shm = tmp_path / "shm"
    (shm / "d").mkdir(parents=True)
    (shm / "d" / "f").write_text("y")
    (shm / "g").write_text("z")
    jail.remove_uid_files(os.getuid(), roots=(shm,))
    assert list(shm.iterdir()) == []


def test_attachment_size_is_the_uploaded_length_not_a_stat_through_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notebook_host.admin as admin_mod

    client, _, _ = _make_app(tmp_path, monkeypatch)
    big = tmp_path / "big"
    big.write_bytes(b"x" * 12345)
    real = admin_mod.write_file_nofollow

    def write_then_swap(path: Path, content: bytes, **kw: Any) -> None:
        real(path, content, **kw)
        path.unlink()
        path.symlink_to(big)  # the jail uid swaps the name right after the rename

    monkeypatch.setattr(admin_mod, "write_file_nofollow", write_then_swap)
    r = client.put(f"/upload/{_mint_data('s', 'd.csv')}", content=b"abc")
    assert r.status_code == 200, r.text
    assert r.json()["size_bytes"] == 3, "no stat of whatever the name points at now"


def test_log_formatter_redacts_tracebacks_too() -> None:
    import io
    import logging
    import logging.config

    from notebook_host.__main__ import uvicorn_log_config

    logging.config.dictConfig(uvicorn_log_config())
    buf = io.StringIO()
    for handler in logging.getLogger().handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(buf)  # pyright: ignore[reportUnknownMemberType]
    try:
        raise ValueError("bad url /n/nb/?access_token=EXC-tok")
    except ValueError:
        logging.getLogger("notebook_host").exception("request failed")
    out = buf.getvalue()
    assert "request failed" in out and "ValueError" in out
    assert "EXC-tok" not in out, "exception text is redacted as well"


def test_every_host_registry_is_private_from_the_moment_its_tmp_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A jailed uid polling data_dir/<registry>.tmp must never be able to open it."""
    from notebook_host import jail
    from notebook_host.blogs_store import BlogRecord, save_blogs
    from notebook_host.consumed_store import save_consumed
    from notebook_host.pids_store import PidRecord, save_pids

    # The worst mode each tmp file had at any point: before any chmod/fchmod
    # (the mode it was created with) and at the rename.
    seen: dict[str, int] = {}
    real_replace, real_chmod, real_fchmod = os.replace, os.chmod, os.fchmod

    def note(name: str, mode: int) -> None:
        seen[name] = seen.get(name, 0) | (mode & 0o777)

    def spy_replace(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        note(Path(dst).name, os.stat(src, follow_symlinks=False).st_mode)
        real_replace(src, dst, *a, **k)

    def spy_chmod(path: Any, mode: int, *a: Any, **k: Any) -> None:
        name = Path(path).name
        if name.endswith(".tmp"):
            note(name, os.stat(path, follow_symlinks=False).st_mode)
        real_chmod(path, mode, *a, **k)

    def spy_fchmod(fd: int, mode: int) -> None:
        note(f"fd:{os.readlink(f'/proc/self/fd/{fd}')}", os.fstat(fd).st_mode)
        real_fchmod(fd, mode)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "chmod", spy_chmod)
    monkeypatch.setattr(os, "fchmod", spy_fchmod)
    old = os.umask(0o022)
    try:
        save_blogs(tmp_path / "blogs.json", {"b": BlogRecord(slug="b", created_at=1.0)})
        save_pids(tmp_path / "pids.json", {"b": PidRecord(slug="b", pid=1, port=1, started_at=1)})
        save_consumed(tmp_path / "consumed.json", {"j": 1})
        jail.save_uid_registry(tmp_path / "uids.json", {"b": 100000})
        jail.get_or_create_slug_uid(tmp_path / "uids.json", "c", start=100000, end=100009)
    finally:
        os.umask(old)
    expected = {
        "blogs.json",
        "pids.json",
        "consumed.json",
        "uids.json",
        "uids.json.cursor",
    }
    assert expected <= set(seen), seen
    assert all(mode & 0o077 == 0 for mode in seen.values()), (
        f"a tmp file was group/world-readable at some point: {seen}"
    )


def test_host_process_runs_with_a_private_umask(monkeypatch: pytest.MonkeyPatch) -> None:
    import notebook_host.__main__ as entry

    monkeypatch.setattr(entry, "load_settings", lambda: unittest.mock.MagicMock(host_port=1))
    monkeypatch.setattr(entry, "create_app", lambda _s: object())
    monkeypatch.setattr(entry.uvicorn, "run", lambda *_a, **_k: None)
    old = os.umask(0o022)
    try:
        entry.main()
        assert os.umask(0o022) == 0o077
    finally:
        os.umask(old)


def test_legacy_slug_root_is_emptied_of_unknown_entries_before_it_opens_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host import jail

    data_dir = tmp_path / "nbs"
    paths = jail.ensure_slug_jail(data_dir, "s")
    paths.root.chmod(0o700)  # a legacy, uid-owned root
    (paths.root / "leak.txt").write_text("secret")
    (paths.root / "leak.txt").chmod(0o644)
    (paths.root / ".notebook.py.tmp").write_text("stale")
    (paths.root / "junkdir").mkdir()
    root_modes: list[int] = []
    real = jail._secure_file  # pyright: ignore[reportPrivateUsage]

    def spy(path: Path, **kw: Any) -> None:
        root_modes.append(paths.root.stat().st_mode & 0o777)
        real(path, **kw)

    monkeypatch.setattr(jail, "_secure_file", spy)
    jail.ensure_slug_jail(data_dir, "s")
    assert root_modes and all(m == 0o700 for m in root_modes), (
        "the legacy root stays closed until its files are checked"
    )
    names = sorted(p.name for p in paths.root.iterdir())
    assert names == ["data", "home", "tmp", "workspace"], names
    assert paths.root.stat().st_mode & 0o777 == 0o711
    root_modes.clear()
    jail.ensure_slug_jail(data_dir, "s")
    assert all(m == 0o711 for m in root_modes), "a converted root is never closed again"


# --- origin-mode cookie hardening ---------------------------


def test_https_origin_cookies_are_host_prefixed_secure_and_hsts_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, seen, hosts = _origin_client(tmp_path, monkeypatch)
    r = client.get(
        "/n/b/",
        headers={"host": hosts["b"], "cookie": "__Host-session_1=abc; session_1=tossed; x=y"},
    )
    cookies = r.headers.get_list("set-cookie")
    assert cookies[0].startswith("__Host-session_1=x"), cookies
    for c in cookies:
        attrs = [a.strip().lower() for a in c.split(";")[1:]]
        assert "secure" in attrs, "cookies only ever travel over https"
        assert "path=/" in attrs and not any(a.startswith("domain") for a in attrs), (
            "__Host- requires Path=/ and no Domain: no sibling origin can set or read it"
        )
    assert r.headers["strict-transport-security"].startswith("max-age=")
    forwarded = seen[-1].headers.get("cookie")
    assert forwarded == "session_1=abc", (
        "only the host-prefixed cookie reaches marimo; a cookie a sibling tossed is dropped"
    )


def test_cookie_domain_strip_tolerates_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.proxy import _response_headers  # pyright: ignore[reportPrivateUsage]

    h = httpx.Headers([("set-cookie", "a=1; Domain = nb.example.com ; Path=/n/b")])
    out = dict(_response_headers(h, own_origin=None, secure=False))
    assert b"domain" not in out[b"set-cookie"].lower()


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
        from urllib.parse import urlsplit

        first = viewer.get(urlsplit(np.url).path, follow_redirects=False)
        assert first.status_code in (200, 303), first.status_code
        assert viewer.get(f"/n/{slug}/").status_code == 200, "the link's cookie opens the page"
        with viewer.websocket_connect(f"/n/{slug}/ws?session_id=s-viewer") as ws:
            assert ws.receive_text(), "the kernel socket authenticates with the same cookie"
    finally:
        kill(np)


def test_shared_link_bootstraps_a_host_only_cookie_and_clean_redirect(tmp_path, monkeypatch):
    from urllib.parse import urlsplit

    client, state, seen, hosts = _origin_client(tmp_path, monkeypatch)
    url = state.processes["b"].url
    response = client.get(urlsplit(url).path, headers={"host": hosts["b"]}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/n/b/"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all(
        cookie.startswith("__Host-") and "Secure" in cookie and "Domain=" not in cookie
        for cookie in cookies
    )
    assert seen[-1].headers["authorization"] == "Bearer tok-b"
    assert not seen[-1].url.query
    assert "access_token=" not in response.headers["location"]


def test_shared_link_is_scoped_to_notebook_token_and_origin(tmp_path, monkeypatch):
    from urllib.parse import urlsplit

    client, state, seen, hosts = _origin_client(tmp_path, monkeypatch)
    path = urlsplit(state.processes["b"].url).path
    assert client.get(path, headers={"host": hosts["a"]}).status_code == 404
    assert (
        client.get(path.replace("/s/b/", "/s/a/"), headers={"host": hosts["a"]}).status_code == 404
    )
    assert (
        client.get(
            path, headers={"host": hosts["b"], "origin": "https://other.example.com"}
        ).status_code
        == 403
    )
    assert not seen
    state.processes["b"].access_token = "new-token"
    assert client.get(path, headers={"host": hosts["b"]}).status_code == 404
    assert not seen


def test_share_link_keys_are_redacted_from_plain_and_encoded_logs():
    from urllib.parse import quote

    from notebook_host.logs import redact_access_token
    from notebook_host.share import share_key

    key = share_key("nb", "marimo-private-token")
    path = f"https://nb.example.com/s/nb/{key}"
    for value in (path, quote(path, safe=""), quote(quote(path, safe=""), safe="")):
        output = redact_access_token(f"GET {value}")
        assert key not in output
        assert "[redacted]" in output
