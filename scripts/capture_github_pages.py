"""Capture production GitHub page renderers with fixed review data."""

from __future__ import annotations

import os
from pathlib import Path

from daimon.adapters.mcp.oauth_github import (
    _already_connected_page,
    _cancelled_page,
    _confirmation_page,
    _done_page,
    _error,
    _expired_page,
    _install_page,
    _Installation,
    _no_repos_page,
    _pending_page,
    _Repo,
)
from playwright.sync_api import Route, sync_playwright
from starlette.responses import Response

ROOT = Path(os.environ.get("GITHUB_CAPTURE_OUT", "/tmp/daimon-github-app/real-pages"))
VIEWPORTS = ((390, 844), (768, 1024), (1280, 900))
NAMES = (
    "research",
    "data-tools",
    "knowledge-base",
    "client-demo",
    "website",
    "reports",
    "research-archive",
    "forecasting",
    "notebooks",
    "client-onboarding",
    "data-pipelines",
    "experiments",
    "analytics",
    "infra",
    "research-api",
    "customer-insights",
    "dashboards",
    "docs",
    "model-library",
    "qa-sandbox",
    "operations",
    "templates",
    "research-tools",
    "playground",
)
REPOS = tuple(
    _Repo(
        id=index + 101, owner_id=55, installation_id=77, full_name=f"example-org/{name}", admin=True
    )
    for index, name in enumerate(NAMES)
)
INSTALLATION = _Installation(
    id=77,
    owner_id=55,
    owner_login="example-org",
    repository_selection="all",
    repos=REPOS,
)


def confirmation(*, agent: bool) -> Response:
    return _confirmation_page(
        root="https://mcp.test",
        state="review",
        invitation_hash="review",
        secret="review",
        cancel_url="https://mcp.test/cancel",
        installations=[INSTALLATION],
        clients_present=True,
        platform="discord",
        workspace="Example Lab",
        agent_name="ResearchBot" if agent else None,
    )


def pages() -> dict[str, Response]:
    return {
        "picker_empty": confirmation(agent=True),
        "picker_many": confirmation(agent=True),
        "picker_search": confirmation(agent=True),
        "picker_selected": confirmation(agent=True),
        "picker_read_only": confirmation(agent=True),
        "picker_connecting": confirmation(agent=True),
        "picker_workspace": confirmation(agent=False),
        "install": _install_page("https://github.com/apps/example/installations/new", "#cancel"),
        "pending": _pending_page("#check", "#cancel"),
        "no_repos": _no_repos_page("https://mcp.test/connect/example", "#another"),
        "done_agent": _done_page(
            count=12,
            platform="discord",
            external_id="123",
            requester_label="Carlos",
            same_person=True,
            agent_name="ResearchBot",
            update_pending=False,
        ),
        "key_pending": _done_page(
            count=12,
            platform="discord",
            external_id="123",
            requester_label="Carlos",
            same_person=True,
            agent_name="ResearchBot",
            update_pending=True,
        ),
        "done_discord": _done_page(
            count=12,
            platform="discord",
            external_id="123",
            requester_label="Carlos",
            same_person=True,
            agent_name=None,
            update_pending=False,
        ),
        "done_slack": _done_page(
            count=12,
            platform="slack",
            external_id="U123",
            requester_label="Carlos",
            same_person=True,
            agent_name=None,
            update_pending=False,
        ),
        "done_forwarded": _done_page(
            count=12,
            platform="discord",
            external_id="123",
            requester_label="Carlos",
            same_person=False,
            agent_name=None,
            update_pending=False,
        ),
        "already": _already_connected_page(
            12, '<a class="gh-primary" href="#back">Back to Discord</a>'
        ),
        "already_agent": _already_connected_page(
            12,
            '<a class="gh-primary" href="#back">Back to Discord</a>',
            agent_name="ResearchBot",
        ),
        "expired_forwarded": _expired_page("Carlos"),
        "cancelled": _cancelled_page('<a class="gh-primary" href="#back">Back to Discord</a>'),
        "unavailable": _error("Couldn't reach GitHub", 502, "#retry"),
        "requester_left": _error(
            "This link was made by someone who's no longer an admin here. "
            "Ask an admin for a new link."
        ),
        "selection_invalid": _error("Selection could not be verified.", 400, "#retry"),
    }


def route_html(content: str):
    def fulfill(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=content)

    return fulfill


def main() -> None:
    states = pages()
    (ROOT / "screenshots").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in VIEWPORTS:
            context = browser.new_context(viewport={"width": width, "height": height})
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin="https://mcp.test"
            )
            for state, response in states.items():
                page = context.new_page()
                content = response.body.decode()
                page.route("https://mcp.test/preview", route_html(content))
                page.goto("https://mcp.test/preview")
                if state in {"picker_selected", "picker_read_only", "picker_connecting"}:
                    for box in page.locator("input[name=repo]").all()[:12]:
                        box.check()
                if state == "picker_empty":
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                if state == "picker_search":
                    page.locator("#search-repos").fill("research")
                    assert page.locator(".repo-choice:visible").count() == 4
                    assert page.locator("#select-all-repos").inner_text() == "Select 4 results"
                    assert page.locator("#clear-search").count() == 0
                if state == "picker_many":
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    last = page.locator(".repo-choice").last.bounding_box()
                    footer = page.locator(".gh-finish").bounding_box()
                    assert last is not None and footer is not None
                    assert last["y"] + last["height"] < footer["y"]
                if state == "picker_read_only":
                    page.locator('input[name="access"][value="read"]').check()
                    assert page.locator("#selection-status").inner_text() == (
                        "12 selected. Read only."
                    )
                if state == "picker_selected":
                    assert page.locator("#selection-status").inner_text() == (
                        "12 selected. Read and write."
                    )
                    assert page.locator("#change-access").is_visible()
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                    page.locator("#change-access").click()
                    assert page.locator('input[name="access"][value="write"]').evaluate(
                        "element => document.activeElement === element"
                    )
                if state == "done_agent":
                    assert page.locator("h1").inner_text() == "Connected 12 repos to ResearchBot"
                    assert (
                        "ResearchBot can use them from your next message."
                        in page.locator("main").inner_text()
                    )
                if state == "picker_connecting":
                    page.locator("#github-connect-form").evaluate(
                        "form => form.addEventListener('submit', "
                        "e => e.preventDefault(), {capture:true})"
                    )
                    page.locator("#connect-repos").click()
                    assert page.locator(".gh-flow.is-connecting").count() == 1
                    assert page.locator("#connect-repos").is_disabled()
                    assert page.locator("#selection-status").inner_text() == "Connecting 12 repos…"
                    assert (
                        page.locator("#github-connect-form").evaluate(
                            "form => new FormData(form).getAll('repo').length"
                        )
                        == 12
                    )
                    assert (
                        page.locator("#github-connect-form").evaluate(
                            "form => new FormData(form).get('access')"
                        )
                        == "write"
                    )
                if state == "picker_empty":
                    assert page.locator("input[name=repo]:checked").count() == 0
                    assert page.locator("#connect-repos").is_disabled()
                    assert page.locator(".gh-repo-list").evaluate(
                        "element => element.scrollHeight === element.clientHeight"
                    )
                    page.locator("#search-repos").fill("research")
                    page.locator("#select-all-repos").click()
                    assert page.locator("input[name=repo]:checked").count() == 4
                    page.locator("#show-selected").click()
                    assert page.locator(".repo-choice:visible").count() == 4
                    page.locator("#search-repos").fill("")
                    page.locator("#show-selected").click()
                    page.locator("input[name=repo]:checked").evaluate_all(
                        "boxes => boxes.forEach(box => { box.checked = false; "
                        "box.dispatchEvent(new Event('change', {bubbles:true})); })"
                    )
                if state.startswith("picker"):
                    assert page.evaluate("document.documentElement.scrollWidth") <= width
                    assert page.locator(".gh-finish").bounding_box()["y"] < height
                    assert page.locator("#connect-repos").evaluate(
                        "element => getComputedStyle(element).fontFamily"
                    ) == page.locator("body").evaluate(
                        "element => getComputedStyle(element).fontFamily"
                    )
                if not state.startswith("picker"):
                    assert "example-org/" not in page.locator("body").inner_text()
                assert " · " not in page.locator("body").inner_text()
                if state not in {"picker_empty", "picker_selected"}:
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                page.close()
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
