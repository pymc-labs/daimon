"""Real-browser proof that the viewer renders a PDF under its own CSP.

Opens a recipient link in Chromium against a running host and checks that
the PDF draws, that the page reports no policy violation, and that every
request the page makes stays on the host's own origin.

Needs Playwright's Chromium (``uv run --with playwright pytest -m slow ...``).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 60>>stream\n"
    b"0 0 0 rg 50 700 400 100 re f BT /F1 24 Tf 60 650 Td (Report) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def viewer_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    import uvicorn
    from report_host import reports_store
    from report_host.config import load_settings
    from report_host.main import create_app

    port = _free_port()
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", f"http://localhost:{port}/")
    monkeypatch.setenv("DAIMON_REPORT__DATA_DIR", str(tmp_path / "data"))
    settings = load_settings(_env_file=None)

    # The factory runs inside the server thread: the host's SQLite connection
    # must be opened on the thread that uses it.
    server = uvicorn.Server(
        uvicorn.Config(
            lambda: create_app(settings),
            factory=True,
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the host should start"
    try:
        base = f"http://localhost:{port}"
        published = httpx.put(
            f"{base}/admin/reports/acme",
            headers={"authorization": "Bearer admin-secret"},
            json={
                "title": "Acme",
                "tenant_id": "t1",
                "agent_name": "analyst",
                "cap_usd": str(Decimal(5)),
                "seam_token": "seam",
                "recipients": [{"name": "Jane", "label": "reader"}],
            },
        )
        assert published.status_code == 200, published.text
        report_dir = tmp_path / "data" / "acme"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "v1.pdf").write_bytes(_PDF)
        conn = reports_store.connect(tmp_path / "data")
        try:
            reports_store.set_current_pdf(conn, slug="acme", name="v1.pdf")
        finally:
            conn.close()
        yield published.json()["links"][0]["link"]
    finally:
        server.should_exit = True
        thread.join(timeout=30)


@pytest.mark.slow
def test_viewer_renders_the_pdf_under_its_csp_without_leaving_the_origin(viewer_url: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    origin = urlsplit(viewer_url).netloc
    requested: list[str] = []
    console_errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.on("request", lambda r: requested.append(r.url))
            page.on(
                "console",
                lambda m: console_errors.append(m.text) if m.type == "error" else None,
            )
            response = page.goto(viewer_url)
            assert response is not None and response.ok
            headers = response.headers
            page.wait_for_selector("#pages canvas", timeout=20_000)
            page.wait_for_function(
                "() => { const c = document.querySelector('#pages canvas');"
                " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
                " for (let i = 0; i < d.length; i += 4) if (d[i] < 128) return true;"
                " return false; }",
                timeout=20_000,
            )
        finally:
            browser.close()

    assert headers["referrer-policy"] == "no-referrer"
    assert "script-src 'self'" in headers["content-security-policy"]
    foreign = [u for u in requested if urlsplit(u).netloc != origin and not u.startswith("data:")]
    assert foreign == [], f"the viewer must make no third-party request: {foreign}"
    assert console_errors == [], f"no CSP or script errors expected: {console_errors}"
