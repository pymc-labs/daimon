"""Tests for notebook_host.lazy_spawn — start on visit, stop when idle, delete on expiry."""

from __future__ import annotations

import asyncio
import runpy
import subprocess
import time
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from notebook_host.blogs_store import BlogRecord, load_blogs, register_blog, unregister_blog
from notebook_host.jail import SlugPaths, get_slug_paths, remove_slug_tree

set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]

_NOW = 1_800_000_000.0


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ports: int) -> None:
        import notebook_host.lazy_spawn as lazy_mod
        from notebook_host.admin import AdminState
        from notebook_host.config import load_settings

        monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8600")
        monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", str(8600 + ports - 1))
        monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")
        monkeypatch.setenv("DAIMON_NOTEBOOK__WARM_WINDOW_SECONDS", "600")
        set_unjailed_test_env(monkeypatch)
        self.settings = load_settings(_env_file=None)
        self.tmp_path = tmp_path
        self.spawned: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.ready = True

        def spawner(
            slug: str,
            paths: SlugPaths,
            port: int,
            *,
            access_token: str = "",
            mode: str = "edit",
            jail_uid: int | None = None,
        ) -> subprocess.Popen[bytes]:
            self.spawned.append({"slug": slug, "port": port, "mode": mode, "token": access_token})
            proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
            proc.poll.return_value = None
            proc.pid = 7777
            return proc  # type: ignore[return-value]

        async def fake_wait(port: int, slug: str, timeout_s: float, *, access_token: str) -> bool:
            return self.ready

        def fake_kill(np: Any) -> None:
            self.killed.append(np.slug)
            np.process.poll.return_value = 0

        monkeypatch.setattr(lazy_mod, "wait_for_port", fake_wait)
        monkeypatch.setattr(lazy_mod, "kill", fake_kill)
        self.state = AdminState(
            settings=self.settings, processes={}, spawner=spawner, validator=None
        )

    def register(
        self, slug: str, *, expires_at: float | None = None, token: str | None = "tok"
    ) -> SlugPaths:
        paths = get_slug_paths(self.tmp_path, slug)
        paths.notebook.parent.mkdir(parents=True, exist_ok=True)
        paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
        register_blog(
            self.settings.resolved_blogs_file,
            BlogRecord(slug=slug, created_at=1.0, access_token=token, expires_at=expires_at),
        )
        return paths

    def running(self, slug: str, *, registered: bool = True, last_active: float = _NOW) -> Any:
        proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.pid = 7777
        port = 8600 + len(self.state.processes)
        np = self.state.make_process(
            slug, port, proc, access_token="tok", mode="run" if registered else "edit"
        )
        np.registered = registered
        np.last_active = last_active
        self.state.processes[slug] = np
        return np


def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ports: int = 4) -> _Harness:
    return _Harness(tmp_path, monkeypatch, ports=ports)


# ─── ensure_running ──────────────────────────────────────────────────────────


async def test_a_visit_starts_a_registered_notebook_under_its_saved_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-post", expires_at=_NOW + 3600, token="saved-token")

    np = await ensure_running(h.state, "pre-post", now=_NOW)

    assert np is not None, "a registered notebook starts on a visit"
    assert h.spawned[-1]["mode"] == "run", "a started notebook is the read-only app"
    assert h.spawned[-1]["token"] == "saved-token", "the published link keeps working"
    assert np.registered, "a started notebook is stopped again when idle"
    assert h.state.processes["pre-post"] is np


async def test_a_visit_to_an_unregistered_slug_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)

    assert await ensure_running(h.state, "nobody", now=_NOW) is None
    assert h.spawned == [], "only registered notebooks can be started by a visit"


async def test_a_visit_to_an_expired_notebook_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-old", expires_at=_NOW - 1)

    assert await ensure_running(h.state, "pre-old", now=_NOW) is None
    assert h.spawned == [], "an expired notebook stays down until the sweep deletes it"


async def test_a_visit_to_a_running_notebook_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-live")
    live = h.running("pre-live")

    assert await ensure_running(h.state, "pre-live", now=_NOW) is live
    assert h.spawned == [], "a running notebook is not started twice"


async def test_concurrent_visits_start_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-busy")

    first, second = await asyncio.gather(
        ensure_running(h.state, "pre-busy", now=_NOW),
        ensure_running(h.state, "pre-busy", now=_NOW),
    )

    assert first is second, "both visits are served by the same process"
    assert len(h.spawned) == 1, "the slug lock lets only one visit start it"


async def test_a_notebook_registered_before_tokens_gets_one_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-legacy", token=None)

    assert await ensure_running(h.state, "pre-legacy", now=_NOW) is not None
    token = h.spawned[-1]["token"]
    assert token, "a notebook is never served without auth"
    assert load_blogs(h.settings.resolved_blogs_file)["pre-legacy"].access_token == token, (
        "the token is saved so the link survives the next stop"
    )


async def test_a_notebook_with_no_source_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    register_blog(h.settings.resolved_blogs_file, BlogRecord(slug="pre-ghost", created_at=1.0))

    with pytest.raises(HTTPException) as err:
        await ensure_running(h.state, "pre-ghost", now=_NOW)

    assert err.value.status_code == 503
    assert h.spawned == []


async def test_a_start_that_never_gets_ready_is_a_503_and_leaves_nothing_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-slow")
    h.ready = False

    with pytest.raises(HTTPException) as err:
        await ensure_running(h.state, "pre-slow", now=_NOW)

    assert err.value.status_code == 503, "a failed start asks the browser to retry"
    assert "pre-slow" not in h.state.processes, (
        "the half-started process is not left holding a port"
    )
    assert h.killed == ["pre-slow"]


async def test_a_full_pool_stops_the_least_recently_visited_idle_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch, ports=3)
    h.running("editor", registered=False, last_active=_NOW - 9000)
    h.running("pre-recent", last_active=_NOW - 10)
    h.running("pre-stale", last_active=_NOW - 500)
    h.register("pre-new")

    np = await ensure_running(h.state, "pre-new", now=_NOW)

    assert np is not None, "a cold start makes room rather than failing"
    assert h.killed == ["pre-stale"], "the least recently visited read-only notebook goes"
    assert "editor" in h.state.processes, "an editor is never stopped to make room"
    assert "pre-recent" in h.state.processes


async def test_a_full_pool_with_every_notebook_in_use_is_a_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import ensure_running

    h = _harness(tmp_path, monkeypatch, ports=2)
    h.running("editor", registered=False)
    h.running("pre-open").open_sockets = 1
    h.register("pre-new")

    with pytest.raises(HTTPException) as err:
        await ensure_running(h.state, "pre-new", now=_NOW)

    assert err.value.status_code == 503
    assert h.killed == [], "a notebook with an open session is never stopped to make room"


# ─── sweep_once ──────────────────────────────────────────────────────────────


async def test_sweep_stops_an_idle_notebook_but_keeps_its_files_and_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = h.register("pre-idle", expires_at=_NOW + 86400)
    h.running("pre-idle", last_active=_NOW - 601)

    result = await sweep_once(h.state, now=_NOW)

    assert "pre-idle" not in h.state.processes, "an idle notebook gives its port back"
    assert paths.notebook.exists(), "its source stays for the next visit"
    assert "pre-idle" in load_blogs(h.settings.resolved_blogs_file), "and so does its link"
    assert result.stopped == [{"slug": "pre-idle", "reason": "idle"}]
    assert result.reaped == []


async def test_sweep_keeps_a_notebook_with_an_open_websocket_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-open")
    h.running("pre-open", last_active=_NOW - 9000).open_sockets = 1

    await sweep_once(h.state, now=_NOW)

    assert "pre-open" in h.state.processes, "nobody's open session is stopped under them"


async def test_sweep_keeps_a_recently_visited_notebook_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-warm")
    h.running("pre-warm", last_active=_NOW - 599)

    await sweep_once(h.state, now=_NOW)

    assert "pre-warm" in h.state.processes, "inside the warm window it stays running"


async def test_sweep_deletes_an_expired_notebook_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = h.register("pre-done", expires_at=_NOW - 1)
    h.running("pre-done")

    result = await sweep_once(h.state, now=_NOW)

    assert "pre-done" not in h.state.processes
    assert not paths.root.exists(), "an expired notebook's whole tree is deleted"
    assert "pre-done" not in load_blogs(h.settings.resolved_blogs_file), "and its link"
    assert result.reaped == [{"slug": "pre-done", "reason": "expired"}]


async def test_sweep_deletes_an_expired_notebook_that_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = h.register("pre-cold", expires_at=_NOW - 1)

    await sweep_once(h.state, now=_NOW)

    assert not paths.root.exists()
    assert "pre-cold" not in load_blogs(h.settings.resolved_blogs_file)


async def test_sweep_never_deletes_a_blog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = h.register("pre-blog", expires_at=None)

    await sweep_once(h.state, now=_NOW + 10 * 365 * 86400)

    assert paths.notebook.exists(), "a blog is kept until someone deletes it"
    assert "pre-blog" in load_blogs(h.settings.resolved_blogs_file)


async def test_sweep_drops_a_dead_registered_process_without_restarting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = h.register("pre-crashed")
    h.running("pre-crashed").process.poll.return_value = 1

    result = await sweep_once(h.state, now=_NOW)

    assert "pre-crashed" not in h.state.processes, "a dead kernel frees its port"
    assert h.spawned == [], "the next visit restarts it, not the sweep"
    assert paths.notebook.exists(), "a crash is not a delete"
    assert result.stopped == [{"slug": "pre-crashed", "reason": "dead"}]


async def test_sweep_does_not_start_registered_notebooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-cold")

    result = await sweep_once(h.state, now=_NOW)

    assert h.spawned == [], "nothing runs until someone visits"
    assert result.reaped == [] and result.stopped == []


async def test_sweep_reaps_an_editor_past_its_ttl_with_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    paths = get_slug_paths(tmp_path, "editor")
    paths.data.mkdir(parents=True, exist_ok=True)
    (paths.data / "big.nc").write_bytes(b"a" * 1024)
    np = h.running("editor", registered=False)
    np.started_at = time.time() - h.settings.subprocess_ttl_seconds - 1

    result = await sweep_once(h.state, now=_NOW)

    assert "editor" not in h.state.processes
    assert not paths.root.exists(), "the editor's whole tree goes with it"
    assert result.reaped == [{"slug": "editor", "reason": "ttl"}]


async def test_sweep_reaps_a_dead_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    h.running("editor", registered=False).process.poll.return_value = 1

    result = await sweep_once(h.state, now=_NOW)

    assert "editor" not in h.state.processes
    assert result.reaped == [{"slug": "editor", "reason": "dead"}]


async def test_sweep_skips_an_expired_notebook_deleted_under_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete holding the slug lock wins over a sweep that scanned the registry first."""
    from notebook_host.lazy_spawn import sweep_once

    h = _harness(tmp_path, monkeypatch)
    h.register("pre-doomed", expires_at=_NOW - 1)
    lock = h.state.lock_for("pre-doomed")
    await lock.acquire()
    try:
        sweep_task = asyncio.create_task(sweep_once(h.state, now=_NOW))
        for _ in range(5):
            await asyncio.sleep(0)
        unregister_blog(h.settings.resolved_blogs_file, "pre-doomed")
        remove_slug_tree(tmp_path, "pre-doomed", uids_file=h.settings.resolved_uids_file)
    finally:
        lock.release()

    result = await sweep_task

    assert result.reaped == [], "the delete already removed it; the sweep reports nothing"
