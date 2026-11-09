"""Real Chromium journeys for Daimon's own pages.

The caller supplies a locally served application and replaces only external
providers. Receipts live outside the repository so each run can be reviewed.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

from daimon.core.stores import github_access
from daimon.core.stores.security_audit import list_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qa.journeys.evidence import (
    Check,
    Observation,
    RunReport,
    compare_screenshots,
    write_report,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


async def github_connect_journey(
    page: Page,
    *,
    base_url: str,
    invitation_token: str,
    tenant_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    artifact_dir: Path,
    browser_version: str,
    viewport: dict[str, int],
    outcome: str = "connect",
) -> RunReport:
    """Run and always retain a report, including browser evidence on failure."""
    started = datetime.now(UTC)
    try:
        return await _github_connect_journey(
            page,
            base_url=base_url,
            invitation_token=invitation_token,
            tenant_id=tenant_id,
            sessionmaker=sessionmaker,
            artifact_dir=artifact_dir,
            browser_version=browser_version,
            viewport=viewport,
            outcome=outcome,
        )
    except Exception as exc:
        screenshot = artifact_dir / "github-failure.png"
        dom = artifact_dir / "github-failure-dom.json"
        if not page.is_closed():
            try:
                await page.screenshot(path=str(screenshot), full_page=True, timeout=5_000)
                dom.write_text(
                    json.dumps(
                        {
                            "path": page.url.split("?", 1)[0],
                            "body": await page.locator("body").inner_text(),
                        },
                        indent=2,
                    )
                    + "\n"
                )
            except Exception:
                pass  # The report still identifies the missing observation.
        report = RunReport(
            run_id=artifact_dir.name,
            journey=f"github-{outcome}-owned-page",
            layer="owned_page_browser",
            started_at=started,
            finished_at=datetime.now(UTC),
            checks=[
                Check(
                    name="browser journey failed",
                    status="FAIL",
                    detail=f"{type(exc).__name__}: {exc}",
                    observations=[
                        Observation(
                            "browser_dom",
                            True,
                            dom.is_file(),
                            "DOM at failure",
                            dom.name if dom.is_file() else None,
                        ),
                        Observation(
                            "browser_pixel",
                            True,
                            screenshot.is_file(),
                            "render at failure",
                            screenshot.name if screenshot.is_file() else None,
                        ),
                    ],
                )
            ],
        )
        write_report(report, artifact_dir)
        raise


async def _github_connect_journey(
    page: Page,
    *,
    base_url: str,
    invitation_token: str,
    tenant_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    artifact_dir: Path,
    browser_version: str,
    viewport: dict[str, int],
    outcome: str,
) -> RunReport:
    """Select one repo, then verify the chosen browser outcome and store."""
    from playwright.async_api import expect

    started = datetime.now(UTC)
    browser = {"engine": "Chromium", "version": browser_version, "viewport": viewport}
    report = RunReport(
        run_id=artifact_dir.name,
        journey=f"github-{outcome}-owned-page",
        layer="owned_page_browser",
        started_at=started,
    )

    # The connect route is a redirect with no Daimon page to render. Use the
    # browser context's cookie jar, then replace GitHub's OAuth handoff only.
    connect = await page.context.request.get(
        f"{base_url}/oauth/github/connect/{invitation_token}", max_redirects=0
    )
    assert connect.status == 307
    authorize_url = connect.headers["location"]
    assert urlparse(authorize_url).path == "/login/oauth/authorize"
    params = parse_qs(urlparse(authorize_url).query)
    assert params["code_challenge_method"] == ["S256"]
    state = params["state"][0]
    response = await page.goto(
        f"{base_url}/oauth/github/callback?{urlencode({'state': state, 'code': 'qa-code'})}"
    )
    assert response is not None and response.ok
    await expect(page).to_have_url(re.compile(rf"^{re.escape(base_url)}/oauth/github/confirm\?"))
    assert await page.evaluate("document.styleSheets.length") >= 1
    assert await page.locator(".web-mark").evaluate(
        "image => image.complete && image.naturalWidth > 0"
    )
    await expect(page.get_by_role("heading", name="Choose repos")).to_be_visible()
    await expect(page.locator("#selection-status")).to_have_text("0 selected")
    await expect(page.locator("#connect-repos")).to_be_disabled()

    search = page.get_by_role("searchbox", name="Search repos")
    await search.fill("one")
    await expect(page.locator("#repo-count")).to_have_text("1 matching repo")
    await expect(page.locator('input[name="repo"][value="101"]')).to_be_visible()
    await page.locator('input[name="repo"][value="101"]').check()
    await expect(page.locator("#selection-status")).to_have_text("1 selected. Read only.")
    await expect(page.locator("#connect-repos")).to_be_enabled()
    await expect(page.get_by_role("radio", name="Read only")).to_be_checked()

    picker_png = artifact_dir / "github-picker.png"
    picker_dom = artifact_dir / "github-picker-dom.json"
    await page.screenshot(path=str(picker_png), full_page=True)
    picker_dom.write_text(
        json.dumps(
            {
                "browser": browser,
                "heading": await page.locator("h1").inner_text(),
                "search": await search.input_value(),
                "visible_repos": await page.locator(
                    ".gh-choice:visible .gh-repo-name"
                ).all_inner_texts(),
                "selected_repo_ids": await page.locator('input[name="repo"]:checked').evaluate_all(
                    "boxes => boxes.map(box => box.value)"
                ),
                "selection_status": await page.locator("#selection-status").inner_text(),
                "submit_enabled": await page.locator("#connect-repos").is_enabled(),
            },
            indent=2,
        )
        + "\n"
    )
    report.checks.append(
        Check(
            name="picker shows the scoped selection",
            status="PASS",
            detail=f"Chromium {browser_version}, {viewport['width']}x{viewport['height']}",
            observations=[
                Observation(
                    "browser_dom",
                    True,
                    True,
                    "one visible and selected; read access",
                    picker_dom.name,
                ),
                Observation("browser_pixel", True, True, "rendered picker", picker_png.name),
            ],
        )
    )

    if outcome == "cancel":
        await page.get_by_role("link", name="Cancel").click()
        await expect(page.get_by_role("heading", name="No changes saved.")).to_be_visible()
        await expect(page.locator("body")).to_contain_text("You can close this tab.")
        cancelled_png = artifact_dir / "github-cancelled.png"
        cancelled_dom = artifact_dir / "github-cancelled-dom.json"
        await page.screenshot(path=str(cancelled_png), full_page=True)
        cancelled_dom.write_text(
            json.dumps(
                {"browser": browser, "heading": await page.locator("h1").inner_text()},
                indent=2,
            )
            + "\n"
        )
        async with sessionmaker() as session:
            grants = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            events = await list_events(session, tenant_id=tenant_id)
        cancelled_backend = artifact_dir / "github-cancelled-backend.json"
        cancelled_backend.write_text(
            json.dumps(
                {
                    "authorized_repos": [grant.repo_id for grant in grants],
                    "github_connect_events": [
                        event.operation for event in events if event.operation == "github_connect"
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        assert grants == []
        assert [event for event in events if event.operation == "github_connect"] == []
        report.checks.append(
            Check(
                name="cancel leaves no grant or connect audit event",
                status="PASS",
                observations=[
                    Observation("browser_dom", True, True, "No changes saved", cancelled_dom.name),
                    Observation(
                        "browser_pixel", True, True, "rendered cancellation", cancelled_png.name
                    ),
                    Observation(
                        "backend_effect",
                        True,
                        True,
                        "empty grants and connect events",
                        cancelled_backend.name,
                    ),
                ],
            )
        )
        report.finished_at = datetime.now(UTC)
        write_report(report, artifact_dir)
        return report

    assert outcome == "connect", outcome

    await page.locator("#connect-repos").click()
    await expect(page.get_by_role("heading", name="Connected 1 repo")).to_be_visible()
    await expect(page.locator("body")).to_contain_text("You can close this tab.")
    done_png = artifact_dir / "github-connected.png"
    done_dom = artifact_dir / "github-connected-dom.json"
    await page.screenshot(path=str(done_png), full_page=True)
    done_dom.write_text(
        json.dumps(
            {
                "browser": browser,
                "heading": await page.locator("h1").inner_text(),
                "body": await page.locator("body").inner_text(),
            },
            indent=2,
        )
        + "\n"
    )

    async with sessionmaker() as session:
        grants = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
        events = await list_events(session, tenant_id=tenant_id)
    backend = {
        "authorized_repos": [
            {
                "repo_id": grant.repo_id,
                "access": grant.max_access,
                "installation_id": grant.installation_id,
            }
            for grant in grants
        ],
        "github_connect_events": [
            {"operation": event.operation, "repo_ids": event.github_repo_ids}
            for event in events
            if event.operation == "github_connect"
        ],
    }
    backend_path = artifact_dir / "github-backend.json"
    backend_path.write_text(json.dumps(backend, indent=2) + "\n")
    assert backend["authorized_repos"] == [
        {"repo_id": 101, "access": "read", "installation_id": 77}
    ], backend
    assert backend["github_connect_events"] == [
        {"operation": "github_connect", "repo_ids": [101]}
    ], backend
    report.checks.append(
        Check(
            name="connected page agrees with persisted grant",
            status="PASS",
            observations=[
                Observation("browser_dom", True, True, "Connected 1 repo", done_dom.name),
                Observation("browser_pixel", True, True, "rendered confirmation", done_png.name),
                Observation(
                    "backend_effect",
                    True,
                    True,
                    "one read grant and audit event",
                    backend_path.name,
                ),
            ],
        )
    )
    # Reopening a consumed invitation is a real browser route. It must show
    # the used-link outcome without adding another grant or audit event.
    reused_response = await page.goto(f"{base_url}/oauth/github/connect/{invitation_token}")
    assert reused_response is not None and reused_response.ok
    await expect(page.get_by_role("heading", name="Already connected: 1 repo.")).to_be_visible()
    reused_png = artifact_dir / "github-used-link.png"
    reused_dom = artifact_dir / "github-used-link-dom.json"
    await page.screenshot(path=str(reused_png), full_page=True)
    reused_dom.write_text(
        json.dumps({"browser": browser, "body": await page.locator("body").inner_text()}, indent=2)
        + "\n"
    )
    async with sessionmaker() as session:
        reused_grants = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
        reused_events = await list_events(session, tenant_id=tenant_id)
    reused_backend = artifact_dir / "github-used-link-backend.json"
    reused_backend.write_text(
        json.dumps(
            {
                "authorized_repos": [grant.repo_id for grant in reused_grants],
                "github_connect_events": [
                    event.operation
                    for event in reused_events
                    if event.operation == "github_connect"
                ],
            },
            indent=2,
        )
        + "\n"
    )
    assert [grant.repo_id for grant in reused_grants] == [101]
    assert [event.operation for event in reused_events if event.operation == "github_connect"] == [
        "github_connect"
    ]
    report.checks.append(
        Check(
            name="used invitation shows existing connection without another write",
            status="PASS",
            observations=[
                Observation(
                    "browser_dom", True, True, "Already connected: 1 repo", reused_dom.name
                ),
                Observation("browser_pixel", True, True, "rendered used link", reused_png.name),
                Observation(
                    "backend_effect",
                    True,
                    True,
                    "one unchanged grant and event",
                    reused_backend.name,
                ),
            ],
        )
    )
    # Test the image comparator against a deliberate browser-only mutation.
    # The unmodified screenshot is a test control, not a human accepted baseline.
    await page.locator("h1").evaluate("heading => heading.textContent = 'Changed test heading'")
    changed_png = artifact_dir / "github-deliberate-change.png"
    await page.screenshot(path=str(changed_png), full_page=True)
    comparison = compare_screenshots(
        changed_png, reused_png, artifact_dir, "github-deliberate-change"
    )
    assert comparison.status == "FAIL" and comparison.changed is True
    assert comparison.changed_pixels is not None and comparison.changed_pixels > 0
    assert comparison.artifact is not None
    report.checks.append(
        Check(
            name="deliberate rendered change is flagged",
            status="PASS",
            detail="Unmodified browser screenshot is a test control, not an accepted baseline.",
            observations=[
                Observation(
                    "browser_pixel",
                    True,
                    True,
                    f"{comparison.changed_pixels} changed pixels; side-by-side diff",
                    Path(comparison.artifact).name,
                )
            ],
        )
    )
    report.finished_at = datetime.now(UTC)
    write_report(report, artifact_dir)
    return report
