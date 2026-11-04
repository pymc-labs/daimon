"""Real Chromium journey across report-host's assembled app and SQLite state."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
import uvicorn
from report_host import reports_store
from report_host.config import load_settings
from report_host.main import create_app

from qa.journeys.evidence import Check, Observation, RunReport, create_artifact_dir, write_report

if TYPE_CHECKING:
    from playwright.async_api import Page, Response, Route

PDFJS_BASE = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/"
PDF_TEXT = "QA report body: Revenue 42"


def _one_page_pdf(text: str) -> bytes:
    """Produce a local PDF with visible body text, without a PDF dependency."""
    stream = f"BT /F1 18 Tf 60 730 Td ({text}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(data)


async def _run_page(
    page: Page,
    *,
    base_url: str,
    viewport_name: str,
    browser_version: str,
    artifact_dir: Path,
    store: sqlite3.Connection,
    pdf_path: Path,
    response_codes: dict[str, int],
) -> RunReport:
    from playwright.async_api import expect

    report = RunReport(
        run_id=artifact_dir.name,
        journey=f"report-host-reader-{viewport_name}",
        layer="owned_page_browser",
        started_at=datetime.now(UTC),
    )
    screenshot = artifact_dir / "report-viewer.png"
    report_page_screenshot = artifact_dir / "report-page.png"
    dom = artifact_dir / "report-viewer-dom.json"
    backend = artifact_dir / "report-backend.json"
    try:
        response = await page.goto(f"{base_url}/r/qa-report?k=local-reader")
        assert response is not None and response.status == 200
        await expect(page.locator("#title")).to_have_text("QA revenue report")
        await expect(page.locator("#reader")).to_have_text("Reading as Local reader")
        await expect(page.locator("#meter")).to_contain_text("$0.00 of $5.00")
        await expect(page.locator("#versions button")).to_have_text("original")
        await expect(page.locator("#pages canvas")).to_have_count(1, timeout=20_000)
        canvas_ink_script = """() => {
            const canvas = document.querySelector('#pages canvas');
            if (!canvas) return 0;
            const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            let ink = 0;
            for (let i = 0; i < d.length; i += 4)
                if (d[i] < 200 && d[i+1] < 200 && d[i+2] < 200 && d[i+3] > 0) ink++;
            return ink;
        }"""
        await page.wait_for_function(f"({canvas_ink_script})() > 100", timeout=20_000)
        canvas_ink = await page.evaluate(canvas_ink_script)
        assert canvas_ink > 100, canvas_ink
        await page.screenshot(
            path=str(report_page_screenshot), full_page=True, animations="disabled"
        )
        if viewport_name == "mobile":
            await page.get_by_role("button", name="Open chat").click()
            await expect(page.locator("body")).to_have_class("dock-open")
        else:
            await expect(page.locator(".dock")).to_be_visible()
        await expect(
            page.get_by_placeholder("Ask about any number or claim in this report")
        ).to_be_in_viewport()
        await page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
        dom.write_text(
            json.dumps(
                {
                    "browser": {
                        "engine": "Chromium",
                        "version": browser_version,
                        "viewport": page.viewport_size,
                    },
                    "title": await page.locator("#title").inner_text(),
                    "reader": await page.locator("#reader").inner_text(),
                    "meter": await page.locator("#meter").inner_text(),
                    "versions": await page.locator("#versions button").all_inner_texts(),
                    "canvas_ink_pixels": canvas_ink,
                    "responses": response_codes,
                },
                indent=2,
            )
            + "\n"
        )
        conn = store
        saved = reports_store.load_report(conn, slug="qa-report")
        assert saved is not None
        revisions = reports_store.list_revisions(conn, slug="qa-report")
        backend.write_text(
            json.dumps(
                {
                    "title": saved.title,
                    "current_pdf": saved.current_pdf,
                    "cap_usd": str(saved.cap_usd),
                    "spent_usd": str(saved.spent_usd),
                    "revisions": [revision.name for revision in revisions],
                    "pdf_bytes": pdf_path.stat().st_size,
                },
                indent=2,
            )
            + "\n"
        )
        assert (saved.title, saved.current_pdf, saved.cap_usd, saved.spent_usd) == (
            "QA revenue report",
            "original.pdf",
            Decimal("5"),
            Decimal("0"),
        )
        assert [revision.name for revision in revisions] == ["original.pdf"]
        for path in (
            "/viewer/app.css",
            "/viewer/app.js",
            "/api/qa-report/state",
            "/files/qa-report/original.pdf",
        ):
            assert response_codes.get(path) == 200, response_codes
        report.checks.append(
            Check(
                name="viewer renders persisted PDF, sidebar and budget",
                status="PASS",
                detail="Real local create_app and SQLite; only exact PDF.js 4.10.38 CDN assets allowed outside fixture; no MCP/model calls.",
                observations=[
                    Observation(
                        "browser_dom",
                        True,
                        True,
                        "title, reader, meter, version and canvas",
                        dom.name,
                    ),
                    Observation(
                        "browser_pixel", True, True, "rendered report page", screenshot.name
                    ),
                    Observation(
                        "browser_pixel",
                        True,
                        True,
                        "report body before opening chat",
                        report_page_screenshot.name,
                    ),
                    Observation(
                        "backend_effect", True, True, "SQLite report and revision", backend.name
                    ),
                ],
            )
        )
        report.finished_at = datetime.now(UTC)
        write_report(report, artifact_dir)
        return report
    except Exception as exc:
        if not page.is_closed():
            try:
                await page.screenshot(path=str(screenshot), full_page=True)
                dom.write_text(
                    json.dumps(
                        {
                            "url": page.url,
                            "body": await page.locator("body").inner_text(),
                            "responses": response_codes,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            except Exception:
                pass
        report.checks.append(
            Check(
                name="viewer journey failed",
                status="FAIL",
                detail=f"{type(exc).__name__}: {exc}",
                observations=[
                    Observation(
                        "browser_dom",
                        True,
                        dom.is_file(),
                        "failure DOM",
                        dom.name if dom.is_file() else None,
                    ),
                    Observation(
                        "browser_pixel",
                        True,
                        screenshot.is_file(),
                        "failure screenshot",
                        screenshot.name if screenshot.is_file() else None,
                    ),
                ],
            )
        )
        report.finished_at = datetime.now(UTC)
        write_report(report, artifact_dir)
        raise


@pytest.mark.slow
@pytest.mark.parametrize(
    "viewport_name, viewport",
    [
        ("desktop", {"width": 1280, "height": 900}),
        ("mobile", {"width": 390, "height": 844}),
    ],
)
@pytest.mark.asyncio
async def test_report_host_real_viewer_matches_persisted_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    viewport_name: str,
    viewport: dict[str, int],
) -> None:
    playwright = pytest.importorskip("playwright.async_api")
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "local-test-only")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://127.0.0.1:9")
    settings = load_settings(_env_file=None).model_copy(
        update={"data_dir": tmp_path / "report-data"}
    )
    conn = reports_store.connect(settings.data_dir)
    reports_store.save_report(
        conn,
        slug="qa-report",
        title="QA revenue report",
        tenant_id="local",
        agent_name="local",
        cap_usd=Decimal("5"),
        agent_token="local",
        now=datetime.now(UTC),
    )
    reports_store.add_recipient(
        conn,
        slug="qa-report",
        name="Local reader",
        label="reader",
        token="local-reader",
        now=datetime.now(UTC),
        ttl_days=1,
    )
    reports_store.add_revision(
        conn,
        slug="qa-report",
        name="original.pdf",
        by_thread=None,
        note="published",
        now=datetime.now(UTC),
    )
    reports_store.set_current_pdf(conn, slug="qa-report", name="original.pdf")
    pdf = settings.data_dir / "qa-report" / "original.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(_one_page_pdf(PDF_TEXT))
    app = create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    artifact_root = (
        Path(os.environ["DAIMON_QA_ARTIFACT_ROOT"])
        if os.environ.get("DAIMON_QA_ARTIFACT_ROOT")
        else tmp_path
    )
    artifact_dir = create_artifact_dir(
        f"report-host-{viewport_name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}",
        root=artifact_root,
    )
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.02)
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context(viewport=viewport)
                blocked: list[str] = []
                response_codes: dict[str, int] = {}

                async def allowed_route(route: Route) -> None:
                    url = route.request.url
                    if url.startswith(base_url + "/") or url in (
                        PDFJS_BASE + "pdf.min.mjs",
                        PDFJS_BASE + "pdf.worker.min.mjs",
                    ):
                        await route.continue_()
                    else:
                        blocked.append(url)
                        await route.abort()

                await context.route("**/*", allowed_route)
                page = await context.new_page()

                def record_response(response: Response) -> None:
                    if response.url.startswith(base_url):
                        response_codes[urlparse(response.url).path] = response.status

                page.on("response", record_response)
                report = await _run_page(
                    page,
                    base_url=base_url,
                    viewport_name=viewport_name,
                    browser_version=browser.version,
                    artifact_dir=artifact_dir,
                    store=conn,
                    pdf_path=pdf,
                    response_codes=response_codes,
                )
                assert report.finalize() == "PASS"
                assert blocked == [], blocked
            finally:
                await browser.close()
        print(f"report-host QA artifacts: {artifact_dir}")
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        sock.close()
        conn.close()
