"""Real-browser proof that per-notebook origins isolate notebooks from each other.

A viewer opens notebook B's link (so the browser holds B's session cookie),
then opens notebook A, whose page runs attacker JavaScript: cross-origin
``fetch`` with credentials, a ``no-cors`` form-style POST, and a WebSocket to
B's kernel, before and after ``history.replaceState`` to B's path. None of it
may reach B's marimo. B's own page, as a control, can open its socket.

Needs Playwright's Chromium (``uv run --with playwright pytest -m slow ...``)
and resolves ``*.localhost`` to 127.0.0.1 the way Chromium does, so no
/etc/hosts edits are required.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import runpy
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]

_NB = """import marimo

app = marimo.App()


@app.cell
def _():
    import marimo as mo

    mo.md("notebook %s")
    return


if __name__ == "__main__":
    app.run()
"""

_ATTACK = """
async ({b, bws}) => {
  const out = {};
  const attempt = async (tag) => {
    try {
      const r = await fetch(b + "/n/b/api/status", {
        method: "POST", credentials: "include",
        headers: {"content-type": "application/json"}, body: "{}",
      });
      out[tag + "_cors"] = r.status;
    } catch (e) { out[tag + "_cors"] = "blocked"; }
    try {
      await fetch(b + "/n/b/api/status", {
        method: "POST", mode: "no-cors", credentials: "include", body: "{}",
      });
      out[tag + "_nocors"] = "sent";
    } catch (e) { out[tag + "_nocors"] = "blocked"; }
    out[tag + "_ws"] = await new Promise((res) => {
      const ws = new WebSocket(bws + "/n/b/ws?session_id=evil-" + tag);
      ws.onopen = () => { res("open"); ws.close(); };
      ws.onerror = () => res("error");
      ws.onclose = () => res("closed");
      setTimeout(() => res("timeout"), 5000);
    });
  };
  await attempt("plain");
  history.replaceState(null, "", "/n/b/");
  await attempt("replaced");
  return out;
}
"""

_OWN_SOCKET = """
async (ws_base) => await new Promise((res) => {
  const ws = new WebSocket(ws_base + "/n/b/ws?session_id=own");
  ws.onopen = () => { res("open"); ws.close(); };
  ws.onerror = () => res("error");
  setTimeout(() => res("timeout"), 10000);
})
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mint(secret: str, op: str, slug: str, jti: str) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {
        "slug": slug,
        "op": op,
        "name": None,
        "max_bytes": 1_000_000,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "jti": jti,
    }
    p = b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{p}.{b64(hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest())}"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    import uvicorn
    from notebook_host import proxy
    from notebook_host.config import load_settings
    from notebook_host.main import create_app

    port = _free_port()
    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__HOST_PORT", str(port))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ORIGIN_BASE", f"localhost:{port}")
    monkeypatch.setenv("DAIMON_NOTEBOOK__ORIGIN_SCHEME", "http")
    monkeypatch.setenv("DAIMON_NOTEBOOK__VALIDATE_ON_PUBLISH", "false")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8193")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8196")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "60")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)

    # Everything the proxy forwards to a marimo backend.
    reached: list[str] = []
    real_request = httpx.AsyncClient.request
    real_connect = proxy.websockets.connect

    async def spy_request(self: httpx.AsyncClient, method: str, url: Any, **kw: Any) -> Any:
        reached.append(f"{method} {url}")
        return await real_request(self, method, url, **kw)

    def spy_connect(url: str, *a: Any, **kw: Any) -> Any:
        reached.append(f"WS {url}")
        return real_connect(url, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "request", spy_request)
    monkeypatch.setattr(proxy.websockets, "connect", spy_connect)

    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    urls: dict[str, str] = {}
    with httpx.Client(timeout=90) as c:
        for i, slug in enumerate(("a", "b")):
            token = _mint(settings.admin_secrets[0].get_secret_value(), "notebook", slug, str(i))
            r = c.put(f"http://127.0.0.1:{port}/upload/{token}", content=_NB % slug)
            assert r.status_code == 200, r.text
            urls[slug] = r.json()["url"]
    try:
        yield {"urls": urls, "reached": reached}
    finally:
        server.should_exit = True
        thread.join(timeout=30)


@pytest.mark.slow
def test_a_notebooks_page_cannot_reach_another_notebook_in_a_real_browser(
    host: dict[str, Any],
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    urls: dict[str, str] = host["urls"]
    reached: list[str] = host["reached"]
    b_origin = urls["b"].split("/n/b/", 1)[0]
    a_origin = urls["a"].split("/n/a/", 1)[0]
    assert a_origin != b_origin and a_origin.endswith(".localhost:" + a_origin.rsplit(":", 1)[1])

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            page_b = context.new_page()
            assert page_b.goto(urls["b"]).ok, "the viewer opens B's link; B's cookie is set"
            own = page_b.evaluate(_OWN_SOCKET, b_origin.replace("http", "ws", 1))
            assert own == "open", "control: B's own page opens B's kernel socket"

            page_a = context.new_page()
            assert page_a.goto(urls["a"]).ok
            before = len(reached)
            out = page_a.evaluate(
                _ATTACK, {"b": b_origin, "bws": b_origin.replace("http", "ws", 1)}
            )
        finally:
            browser.close()

    assert out["plain_cors"] in ("blocked", 403), out
    assert out["replaced_cors"] in ("blocked", 403), out
    assert out["plain_ws"] != "open" and out["replaced_ws"] != "open", out
    leaked = [r for r in reached[before:] if ":8193" in r or ":8194" in r]
    b_port_hits = [r for r in leaked if "/n/b/" in r]
    assert b_port_hits == [], f"A's page reached B's marimo: {b_port_hits}"
