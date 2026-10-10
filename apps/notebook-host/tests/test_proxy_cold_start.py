"""The proxy starts a stopped registered notebook on a visit and records activity."""

from __future__ import annotations

import runpy
import subprocess
import time
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from notebook_host.blogs_store import BlogRecord, register_blog
from notebook_host.jail import SlugPaths, get_slug_paths
from notebook_host.lifecycle import origin_label_for

set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]


class _Proxy:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, origin_base: str | None
    ) -> None:
        import notebook_host.lazy_spawn as lazy_mod
        from notebook_host.admin import AdminState
        from notebook_host.config import load_settings
        from notebook_host.proxy import create_proxy_router

        monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8800")
        monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8803")
        if origin_base is not None:
            monkeypatch.setenv("DAIMON_NOTEBOOK__ORIGIN_BASE", origin_base)
        set_unjailed_test_env(monkeypatch)
        self.settings = load_settings(_env_file=None)
        self.tmp_path = tmp_path
        self.spawned: list[str] = []
        self.ready = True
        self.cookie = False
        self.forwarded: list[httpx.Request] = []

        def spawner(slug: str, paths: SlugPaths, port: int, **_kw: Any) -> subprocess.Popen[bytes]:
            self.spawned.append(slug)
            proc: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
            proc.poll.return_value = None
            proc.pid = 4242
            return proc  # type: ignore[return-value]

        async def fake_wait(*_args: object, **_kwargs: object) -> bool:
            return self.ready

        def fake_kill(np: Any) -> None:
            np.process.poll.return_value = 0

        monkeypatch.setattr(lazy_mod, "wait_for_port", fake_wait)
        monkeypatch.setattr(lazy_mod, "kill", fake_kill)

        def handler(request: httpx.Request) -> httpx.Response:
            self.forwarded.append(request)
            return httpx.Response(
                200,
                text="ok",
                headers={"set-cookie": "session=x; Path=/n/pre-post/; HttpOnly"}
                if self.cookie
                else {},
            )

        real_client = httpx.AsyncClient

        def fake_client(**kwargs: Any) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("notebook_host.proxy.httpx.AsyncClient", fake_client)
        self.state = AdminState(settings=self.settings, processes={}, spawner=spawner)
        app = FastAPI()
        app.include_router(create_proxy_router(self.state))
        self.client = TestClient(app)

    def register(self, slug: str, *, token: str = "tok", expires_at: float | None = None) -> None:
        paths = get_slug_paths(self.tmp_path, slug)
        paths.notebook.parent.mkdir(parents=True, exist_ok=True)
        paths.notebook.write_text("import marimo as mo\napp = mo.App()", encoding="utf-8")
        register_blog(
            self.settings.resolved_blogs_file,
            BlogRecord(slug=slug, created_at=1.0, access_token=token, expires_at=expires_at),
        )


def test_a_visit_to_a_stopped_notebook_starts_it_and_serves_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-post")

    r = p.client.get("/n/pre-post/?access_token=tok")

    assert r.status_code == 200, r.text
    assert p.spawned == ["pre-post"], "the visit started the notebook"
    assert len(p.forwarded) == 1, "and the same request was served by it"


@pytest.mark.parametrize("query", ["", "?access_token=wrong", "?access_token="])
def test_a_visit_without_the_notebooks_token_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Slugs are visible to every notebook on the host, so they cannot authorize a start.

    Starting one can stop another tenant's idle notebook to free a port, so
    anyone who could start notebooks by slug could keep evicting others.
    """
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-post")

    assert p.client.get(f"/n/pre-post/{query}").status_code == 404
    assert p.spawned == [], "only the link holder can start a stopped notebook"


def test_a_websocket_never_starts_a_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocketDisconnect

    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-post")

    with (
        pytest.raises(WebSocketDisconnect),
        p.client.websocket_connect("/n/pre-post/ws") as ws,
    ):
        ws.receive_text()
    assert p.spawned == [], "a socket carries no token; the page load starts the notebook"


def test_a_visit_to_an_unknown_slug_is_a_404_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)

    assert p.client.get("/n/nobody/").status_code == 404
    assert p.spawned == []


def test_a_visit_to_an_expired_notebook_is_a_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-old", expires_at=time.time() - 1)

    assert p.client.get("/n/pre-old/").status_code == 404
    assert p.spawned == []


def test_a_start_that_fails_asks_the_browser_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-slow")
    p.ready = False

    r = p.client.get("/n/pre-slow/?access_token=tok")

    assert r.status_code == 503
    assert r.headers["retry-after"], "the browser is told when to try again"


def test_a_visit_records_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-post")
    p.client.get("/n/pre-post/?access_token=tok")
    np = p.state.processes["pre-post"]
    np.last_active = 0.0

    p.client.get("/n/pre-post/api/status")

    assert np.last_active > time.time() - 5, "every forwarded request keeps it warm"


def test_origin_mode_starts_a_notebook_only_on_its_own_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _Proxy(tmp_path, monkeypatch, origin_base="nb.example.com")
    p.register("pre-a", token="tok-a")
    p.register("pre-b", token="tok-b")
    own_host = f"{origin_label_for('tok-b')}.nb.example.com"
    other_host = f"{origin_label_for('tok-a')}.nb.example.com"

    assert p.client.get("/n/pre-b/", headers={"host": "nb.example.com"}).status_code == 404
    assert p.client.get("/n/pre-b/", headers={"host": other_host}).status_code == 404
    assert p.spawned == [], "a request on the wrong origin never starts a notebook"

    assert p.client.get("/n/pre-b/", headers={"host": own_host}).status_code == 404, (
        "the origin label is visible in TLS SNI, so it alone does not start a notebook"
    )
    r = p.client.get("/n/pre-b/?access_token=tok-b", headers={"host": own_host})
    assert r.status_code == 200
    assert p.spawned == ["pre-b"]


def test_an_open_websocket_is_counted_until_it_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import notebook_host.proxy as proxy_mod

    p = _Proxy(tmp_path, monkeypatch, origin_base=None)
    p.register("pre-ws")
    seen_open: list[int] = []

    class _Backend:
        async def __aenter__(self) -> _Backend:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def __aiter__(self) -> _Backend:
            return self

        async def __anext__(self) -> str:
            seen_open.append(p.state.processes["pre-ws"].open_sockets)
            await asyncio.sleep(3600)
            raise StopAsyncIteration

        async def send(self, _msg: object) -> None:
            return None

    monkeypatch.setattr(proxy_mod.websockets, "connect", lambda *_a, **_k: _Backend())

    p.client.get("/n/pre-ws/?access_token=tok")
    with p.client.websocket_connect("/n/pre-ws/ws") as ws:
        ws.send_text("hello")
        deadline = time.time() + 5
        while not seen_open and time.time() < deadline:
            time.sleep(0.01)
    deadline = time.time() + 5
    while p.state.processes["pre-ws"].open_sockets and time.time() < deadline:
        time.sleep(0.01)

    assert seen_open == [1], "an open session holds the notebook awake"
    assert p.state.processes["pre-ws"].open_sockets == 0, "and stops holding it once closed"


def test_a_share_link_starts_a_stopped_notebook_without_a_raw_token_in_the_url(
    tmp_path, monkeypatch
):
    from notebook_host.share import share_key

    proxy = _Proxy(tmp_path, monkeypatch, origin_base=None)
    proxy.register("pre-post")
    proxy.cookie = True
    result = proxy.client.get(f"/s/pre-post/{share_key('pre-post', 'tok')}", follow_redirects=False)
    assert result.status_code == 303
    assert result.headers["location"] == "/n/pre-post/"
    assert proxy.spawned == ["pre-post"]
    assert proxy.forwarded[0].headers["authorization"] == "Bearer tok"
    assert not proxy.forwarded[0].url.query


def test_an_invalid_share_link_cannot_start_a_notebook(tmp_path, monkeypatch):
    proxy = _Proxy(tmp_path, monkeypatch, origin_base=None)
    proxy.register("pre-post")
    assert proxy.client.get("/s/pre-post/wrong").status_code == 404
    assert proxy.spawned == []
    assert proxy.forwarded == []


def test_share_link_does_not_redirect_without_a_successful_cookie_handshake(tmp_path, monkeypatch):
    from notebook_host.share import share_key

    proxy = _Proxy(tmp_path, monkeypatch, origin_base=None)
    proxy.register("pre-post")
    result = proxy.client.get(f"/s/pre-post/{share_key('pre-post', 'tok')}", follow_redirects=False)
    assert result.status_code == 503
    assert "location" not in result.headers
