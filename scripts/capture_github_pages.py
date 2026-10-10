"""Capture production GitHub page renderers with fixed review data."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from daimon.adapters.mcp.checkout import billing_cancel, billing_success
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
from daimon.adapters.mcp.oauth_github_personal import _page as personal_page
from daimon.adapters.mcp.oauth_mcp import _success_page as mcp_success_page
from daimon.adapters.mcp.oauth_slack import (
    _install_landing_html as slack_install_page,
)
from daimon.adapters.mcp.oauth_slack import (
    _success_html as slack_success_page,
)
from playwright.sync_api import Route, sync_playwright
from starlette.responses import Response

STATIC = Path(__file__).resolve().parents[1] / "packages/adapters/mcp/daimon/adapters/mcp/static"

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


def confirmation(
    *,
    agent: bool,
    already_added: frozenset[int] = frozenset(),
    needed: dict[int, bool] | None = None,
) -> Response:
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
        already_added=already_added,
        needed=needed,
    )


def pages() -> dict[str, Response]:
    return {
        "picker_empty": confirmation(agent=True),
        "picker_many": confirmation(agent=True),
        "picker_search": confirmation(agent=True),
        "picker_no_results": confirmation(agent=True),
        "picker_error": _confirmation_page(
            root="https://mcp.test",
            state="review",
            invitation_hash="review",
            secret="review",
            cancel_url="https://mcp.test/cancel",
            installations=[INSTALLATION],
            clients_present=True,
            platform="discord",
            workspace="Example Lab",
            agent_name="ResearchBot",
            selection_error="Select at least one repo",
        ),
        "picker_selected": confirmation(agent=True),
        "picker_read_only": confirmation(agent=True),
        "picker_connecting": confirmation(agent=True),
        "picker_workspace": confirmation(agent=False),
        "picker_already_added": confirmation(agent=True, already_added=frozenset({101, 102})),
        "picker_needs_write": confirmation(
            agent=True, already_added=frozenset({102}), needed={101: True}
        ),
        "install": _install_page("https://github.com/apps/example/installations/new", "#cancel"),
        "pending": _pending_page("#check", "#cancel", "example-org"),
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
        "done_key_retired": _done_page(
            count=2,
            platform="slack",
            external_id="U123",
            requester_label="Carlos",
            same_person=True,
            agent_name="ResearchBot",
            update_pending=False,
            retired_saved_key=True,
        ),
        "done_key_waiting": _done_page(
            count=2,
            platform="slack",
            external_id="U123",
            requester_label="Carlos",
            same_person=True,
            agent_name="ResearchBot",
            update_pending=True,
            missing_repos=(("example-org/research", True),),
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
        "slack_install": slack_install_page(
            authorize_url="https://slack.com/oauth/v2/authorize",
            signup_credit=Decimal("5"),
        ),
        "slack_done": slack_success_page(workspace="Example Lab", signup_credit=Decimal("5")),
        "personal_link": personal_page(
            "Linked as @reviewer",
            status=200,
            back='<a href="#back">Back to Discord</a>',
        ),
        "mcp_done": mcp_success_page(server_name="Analytics MCP", agent_name="ResearchBot"),
        "billing_done": asyncio.run(billing_success(None)),  # type: ignore[arg-type]
        "billing_cancel": asyncio.run(billing_cancel(None)),  # type: ignore[arg-type]
    }


def route_html(content: str):
    def fulfill(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=content)

    return fulfill


def route_asset(route: Route) -> None:
    name = Path(urlparse(route.request.url).path).name
    content_type = (
        "text/css"
        if name.endswith(".css")
        else ("image/png" if name.endswith(".png") else "font/ttf")
    )
    route.fulfill(status=200, content_type=content_type, body=(STATIC / name).read_bytes())


def collect_form(posted: list[dict[str, list[str]]]):
    def accept_form(route: Route) -> None:
        posted.append(parse_qs(route.request.post_data or ""))
        route.fulfill(status=200, content_type="text/html", body="Connected")

    return accept_form


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
                page.route("https://mcp.test/web/*", route_asset)
                page.goto("https://mcp.test/preview")
                if state in {"picker_selected", "picker_read_only", "picker_connecting"}:
                    for box in page.locator("input[name=repo]").all()[:12]:
                        box.check()
                if state == "picker_empty":
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                if state == "picker_search":
                    page.locator("#search-repos").fill("research")
                    assert page.locator(".repo-choice:visible").count() == 4
                    assert page.locator("#select-all-repos").inner_text() == "Select all 4"
                    assert page.locator("#clear-search").count() == 0
                if state == "picker_no_results":
                    page.locator("#search-repos").fill("no-such-repo")
                    assert page.locator(".gh-empty").is_visible()
                    assert page.locator("#repo-count").inner_text() == "0 matching repos"
                if state == "picker_error":
                    assert (
                        page.locator(".gh-inline-error").inner_text() == "Select at least one repo"
                    )
                if state == "picker_many":
                    page.locator(".gh-repo-list").evaluate(
                        "element => element.scrollTop = element.scrollHeight"
                    )
                    page.evaluate(
                        "window.scrollTo({top: document.documentElement.scrollHeight, "
                        "behavior: 'instant'})"
                    )
                    last = page.locator(".repo-choice").last.bounding_box()
                    footer = page.locator(".gh-finish").bounding_box()
                    assert last is not None and footer is not None
                    assert last["y"] + last["height"] < footer["y"]
                if state == "picker_read_only":
                    page.locator('input[name="access"][value="read"]').check()
                    assert page.locator("#selection-status").inner_text() == (
                        "12 selected. Read only."
                    )
                    assert page.locator("#connect-repos").inner_text() == "Add 12 repos"
                if state == "picker_selected":
                    assert page.locator("#selection-status").inner_text() == (
                        "12 selected. Read only."
                    )
                    assert page.locator("#change-access").is_visible()
                    page.locator(".gh-repo-list").evaluate("element => element.scrollTop = 0")
                    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                    page.locator("#change-access").click()
                    assert page.locator('input[name="access"][value="read"]').evaluate(
                        "element => document.activeElement === element"
                    )
                if state == "done_agent":
                    assert page.locator("h1").inner_text() == "Added 12 repos to ResearchBot."
                    assert "You can close this tab." in page.locator("main").inner_text()
                    assert "old GitHub token" not in page.locator("main").inner_text()
                if state == "picker_needs_write":
                    assert page.locator("input[name=repo]:checked:not(:disabled)").count() == 1
                    assert page.locator(".gh-added").all_inner_texts() == [
                        "Needs write",
                        "Already added",
                    ]
                    assert page.locator("#connect-repos").inner_text() == "Add 1 repo"
                if state == "picker_already_added":
                    assert page.locator("h1").inner_text() == "Add repos to ResearchBot"
                    assert page.locator("input[name=repo]:disabled:checked").count() == 2
                    assert page.locator(".gh-added").count() == 2
                    assert page.locator("#connect-repos").is_disabled()
                    assert page.locator("#connect-repos").inner_text() == "Add repos"
                    page.locator('input[name="repo"]:not(:disabled)').first.check()
                    assert page.locator("#connect-repos").inner_text() == "Add 1 repo"
                    page.locator('input[name="access"][value="write"]').check()
                    assert page.locator("#audience").inner_text() == (
                        "Anyone who talks to ResearchBot can ask it to read and change them."
                    )
                if state == "picker_connecting":
                    page.locator(".gh-repo-list").evaluate("element => element.scrollTop = 0")
                    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                    page.locator("#github-connect-form").evaluate(
                        "form => form.addEventListener('submit', "
                        "e => e.preventDefault(), {capture:true})"
                    )
                    page.locator("#connect-repos").click()
                    assert page.locator(".gh-flow.is-connecting").count() == 1
                    assert page.locator("#connect-repos").is_disabled()
                    assert page.locator("#selection-status").inner_text() == "Adding 12 repos…"
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
                        == "read"
                    )
                if state == "picker_empty":
                    assert page.locator("input[name=repo]:checked").count() == 0
                    assert page.locator("#connect-repos").is_disabled()
                    assert page.locator(".gh-repo-list").evaluate(
                        "element => element.scrollHeight > element.clientHeight"
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
                if not state.startswith("picker") and state != "done_key_waiting":
                    assert "example-org/" not in page.locator("body").inner_text()
                assert " · " not in page.locator("body").inner_text()
                if state not in {"picker_empty", "picker_selected"}:
                    page.screenshot(path=ROOT / "screenshots" / f"{state}-{width}.png")
                page.close()
            if width == 390:
                no_js = browser.new_context(
                    viewport={"width": width, "height": height}, java_script_enabled=False
                )
                page = no_js.new_page()
                page.route(
                    "https://mcp.test/preview", route_html(confirmation(agent=True).body.decode())
                )
                page.route("https://mcp.test/web/*", route_asset)
                posted: list[dict[str, list[str]]] = []

                page.route("https://mcp.test/oauth/github/confirm", collect_form(posted))
                page.goto("https://mcp.test/preview")
                radio = page.locator('input[name="access"][value="read"]')
                assert radio.is_checked()
                radio.focus()
                page.keyboard.press("ArrowDown")
                assert page.locator('input[name="access"][value="write"]').is_checked()
                page.keyboard.press("ArrowUp")
                assert radio.is_checked()
                first = page.locator('input[name="repo"]').first
                first.focus()
                page.keyboard.press("Space")
                assert first.is_checked()
                page.locator("#connect-repos").click()
                assert posted and posted[0]["repo"] == ["101"]
                assert posted[0]["access"] == ["read"]
                no_js.close()
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
