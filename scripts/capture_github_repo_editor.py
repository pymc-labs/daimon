"""Capture the GitHub repo editor using synthetic data and local assets.

Run: uv run --with playwright python scripts/capture_github_repo_editor.py
Set GITHUB_EDITOR_CAPTURE_OUT to choose the output directory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from daimon.adapters.mcp.oauth_github import (
    _agent_editor_page,
    _error,
    _expired_page,
    _Installation,
    _PendingInstallationRequest,
    _Repo,
    _saved_agent_page,
)
from daimon.core.stores.github_access import AgentRepo
from playwright.sync_api import Route, sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "packages/adapters/mcp/daimon/adapters/mcp/static"
ROOT = Path(os.environ.get("GITHUB_EDITOR_CAPTURE_OUT", "/tmp/daimon-github-page-evidence"))
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


def grant(repo_id: int, name: str, **overrides: object) -> AgentRepo:
    return AgentRepo.model_validate(
        {
            "repo_id": repo_id,
            "full_name": name,
            "scope": "agent",
            "max_access": "write",
            "baseline_access": "read",
            "ceiling_access": "read",
            "staged": False,
            "is_working_repo": False,
            "grant_version": 1,
            "authorization_version": 1,
            "status": "active",
            "added_by_account_id": None,
            "added_at": STAMP,
            "granted_by_account_id": None,
            "granted_at": STAMP,
            **overrides,
        }
    )


def pages() -> dict[str, str]:
    installation = _Installation(
        id=1,
        owner_id=2,
        owner_login="example-org",
        repository_selection="selected",
        repos=tuple(
            _Repo(id=i, owner_id=2, installation_id=1, full_name=f"example-org/{name}", admin=True)
            for i, name in ((11, "analysis"), (12, "datasets"), (13, "reports"), (14, "website"))
        ),
    )
    current = [
        grant(11, "example-org/analysis", is_working_repo=True, ceiling_access="write"),
        grant(12, "example-org/datasets"),
        grant(20, "another-org/shared-library"),
    ]
    base = {
        "root": "https://mcp.test",
        "state": "synthetic-state",
        "invitation_hash": "synthetic-invitation",
        "secret": "synthetic-only",
        "agent_name": "ResearchBot",
        "workspace": "Example workspace",
        "platform": "discord",
        "installations": [installation],
        "snapshot": "[]",
        "cancel_url": "https://mcp.test/cancel",
        "install_url": "https://github.com/apps/example/installations/new",
    }
    states = {
        "current": _agent_editor_page(**base, grants=current),
        "empty": _agent_editor_page(**base, grants=[]),
        "staged": _agent_editor_page(
            **base, grants=[grant(11, "example-org/analysis", staged=True)]
        ),
        "unavailable": _agent_editor_page(
            **{**base, "installations": []},
            grants=[grant(20, "another-org/shared-library", status="suspended")],
        ),
        "pending": _agent_editor_page(
            **{**base, "installations": []},
            grants=current,
            pending_installation=_PendingInstallationRequest(True, "example-org"),
        ),
        "stale": _error(
            "The agent's repos changed. Reload and try again.", 409, "https://mcp.test/reload"
        ),
        "expired": _expired_page("Workspace admin"),
        "saved": _saved_agent_page("ResearchBot", current),
    }
    return {name: response.body.decode() for name, response in states.items()}


def local_asset(route: Route) -> None:
    name = Path(urlparse(route.request.url).path).name
    kind = (
        "text/css"
        if name.endswith(".css")
        else "image/png"
        if name.endswith(".png")
        else "font/ttf"
    )
    route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_bytes())


def html_route(content: str):
    def fulfill(route: Route) -> None:
        route.fulfill(content_type="text/html", body=content)

    return fulfill


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    states = pages()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in ((390, 844), (1280, 1000)):
            for name, content in states.items():
                page = browser.new_page(viewport={"width": width, "height": height})
                errors: list[str] = []
                page.on("pageerror", lambda error, caught=errors: caught.append(str(error)))
                page.route("https://mcp.test/web/*", local_asset)
                page.route(
                    "https://mcp.test/preview",
                    html_route(content),
                )
                page.goto("https://mcp.test/preview")
                if name == "current":
                    save = page.get_by_role("button", name="Save changes")
                    assert save.is_disabled()
                    page.locator('input[name="repo"][value="13"]').check()
                    page.get_by_role("searchbox").fill("website")
                    assert page.locator('input[name="repo"][value="13"]').is_checked()
                    assert not save.is_disabled()
                    page.get_by_role("searchbox").fill("")
                    page.locator('input[name="remove"][value="11"]').check()
                    page.locator('select[name="working"]').select_option("13")
                    selected = page.evaluate(
                        "Object.fromEntries(new FormData(document.querySelector('form')))"
                    )
                    assert selected["repo"] == "13"
                    assert selected["remove"] == "11"
                    assert selected["working"] == "13"
                    assert selected["access"] == "read"
                    page.screenshot(path=ROOT / f"selected-{width}.png", full_page=True)
                    page.reload()
                    if width == 390:
                        page.screenshot(path=ROOT / "mobile-top.png")
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.screenshot(path=ROOT / "mobile-controls.png")
                        page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=ROOT / f"{name}-{width}.png", full_page=True)
                assert not errors, errors
                assert page.evaluate("document.documentElement.scrollWidth") <= width, name
                assert " · " not in page.locator("body").inner_text(), name
                (ROOT / f"{name}.html").write_text(content)
                page.close()
        browser.close()
    print(f"Captured {len(states)} states at two widths in {ROOT}")


if __name__ == "__main__":
    main()
