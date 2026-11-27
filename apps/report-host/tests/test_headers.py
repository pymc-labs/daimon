"""Tests for report_host.headers: Referrer-Policy everywhere, CSP on HTML only."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from report_host import reports_store
from report_host.config import load_settings
from report_host.headers import CONTENT_SECURITY_POLICY
from report_host.main import create_app


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com/")
    monkeypatch.setenv("DAIMON_REPORT__DATA_DIR", str(tmp_path / "data"))
    app = create_app(load_settings(_env_file=None))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.parametrize(
    "path",
    ["/health", "/viewer/app.js", "/viewer/vendor/pdf.min.mjs", "/api/nope/state", "/missing"],
)
async def test_every_response_says_no_referrer(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _client(tmp_path, monkeypatch) as client:
        resp = await client.get(path)
    assert resp.headers.get("referrer-policy") == "no-referrer", f"{path} ({resp.status_code})"


async def test_viewer_html_carries_the_csp_and_json_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _client(tmp_path, monkeypatch) as client:
        reports_store.connect(tmp_path / "data").close()
        html = await client.get("/r/nope", headers={"accept": "text/html"})
        health = await client.get("/health")
    assert html.headers["content-type"].startswith("text/html"), "the unavailable page is HTML"
    assert html.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "content-security-policy" not in health.headers, "JSON needs no document policy"


def test_csp_allows_no_third_party_source_and_no_inline_script() -> None:
    for directive in CONTENT_SECURITY_POLICY.split("; "):
        name, _, sources = directive.partition(" ")
        assert "http:" not in sources and "https:" not in sources and "*" not in sources, name
        if name == "script-src":
            assert "'unsafe-inline'" not in sources and "'unsafe-eval'" not in sources
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY
