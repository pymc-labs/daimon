"""Browser checks for the real server-rendered multi-owner agent picker.

Run: uv run --with playwright==1.58.0 python scripts/check_github_picker.py
Uses synthetic fixture data and the built web.css; no GitHub login or test DB.
"""

from __future__ import annotations

import base64
import io
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from daimon.adapters.mcp.oauth_github import (
    _agent_editor_page,
    _Installation,
    _Repo,
    _saved_agent_page,
)
from daimon.core.stores.github_access import AgentRepo
from PIL import Image
from playwright.sync_api import Page, Route, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "packages/adapters/mcp/daimon/adapters/mcp/static/web.css"
MARK = ROOT / "packages/adapters/mcp/daimon/adapters/mcp/static/daimon-face.png"
CHROME = os.environ.get(
    "CHROME", str(Path.home() / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome")
)
NAVY = "rgb(12, 31, 64)"
NAVY_FILL = NAVY
MUTED = "rgb(86, 100, 122)"
# Semantic roles from the shared palette in static/input.css.
TEAL_BG, TEAL_FG, TEAL_LINE = "rgb(234, 248, 244)", "rgb(11, 107, 92)", "rgb(42, 138, 119)"
GREEN_BG, GREEN_FG = "rgb(232, 245, 230)", "rgb(30, 107, 46)"
RED_BG, RED_FG = "rgb(252, 237, 237)", "rgb(159, 45, 45)"
AMBER_BG, AMBER_FG = "rgb(253, 240, 213)", "rgb(138, 83, 0)"
MUTED_BG = "rgb(238, 242, 243)"
SHOTS = Path("/home/derick/scratch/daimon-github-selector/after")


def grant(repo_id: int, name: str, *, working: bool = False) -> AgentRepo:
    now = datetime.now(UTC)
    return AgentRepo(
        repo_id=repo_id,
        full_name=name,
        scope="agent",
        max_access="write",
        baseline_access="write",
        ceiling_access="write",
        staged=False,
        status="active",
        added_by_account_id=None,
        added_at=now,
        granted_by_account_id=None,
        granted_at=now,
        is_working_repo=working,
    )


def inline_assets(page_html: str) -> str:
    mark = base64.b64encode(MARK.read_bytes()).decode()
    page_html = page_html.replace("/web/daimon-face.png", f"data:image/png;base64,{mark}")
    # Exercise the image markup and shapes without a network request in tests.
    page_html = re.sub(
        r'https://avatars\.githubusercontent\.com/[^" ]+',
        f"data:image/png;base64,{mark}",
        page_html,
    )
    css = CSS.read_text()
    for weight in ("Regular", "SemiBold", "Bold"):
        font = CSS.with_name(f"Inter-{weight}.ttf")
        encoded = base64.b64encode(font.read_bytes()).decode()
        for url in (f"url('./Inter-{weight}.ttf')", f"url(./Inter-{weight}.ttf)"):
            css = css.replace(url, f"url(data:font/ttf;base64,{encoded})")
    return re.sub(
        r'<link rel="stylesheet" href="[^"]+">',
        lambda _: f"<style>{css}</style>",
        page_html,
        count=1,
    )


def fixture_page(
    *,
    one_owner: bool = False,
    error_owner: bool = False,
    stale: bool = False,
    three_granted: bool = False,
    no_avatars: bool = False,
    truncated: bool = False,
    suspended: bool = False,
) -> str:
    owners = (
        (77, 55, "sam-rivera", "User"),
        (78, 56, "northwind-labs", "Organization"),
        (79, 57, "tidewater-eap", "Organization"),
    )
    if one_owner:
        owners = owners[:1]
    installations = [
        _Installation(
            id=installation_id,
            owner_id=owner_id,
            owner_login=login,
            repository_selection="selected",
            owner_type=owner_type,
            avatar_url=(
                f"https://avatars.githubusercontent.com/u/{owner_id}"
                if installation_id in (77, 78) and not no_avatars
                else None
            ),
            fetch_state="error" if error_owner and installation_id == 79 else "ok",
            state="suspended" if suspended and installation_id == 79 else "active",
            truncated=truncated and installation_id == 78,
            repos=tuple(
                _Repo(repo_id, owner_id, installation_id, f"{login}/{name}", "admin")
                for repo_id, name in (
                    (installation_id * 100 + 1, "saved"),
                    (installation_id * 100 + 2, "new"),
                )
            )
            if not (error_owner and installation_id == 79)
            else (),
        )
        for installation_id, owner_id, login, owner_type in owners
    ]
    grants = (
        [
            grant(7801, "northwind-labs/saved", working=True),
            grant(7701, "sam-rivera/saved"),
        ]
        if not one_owner
        else [grant(7701, "sam-rivera/saved", working=True)]
    )
    if stale:
        grants.append(grant(9901, "lost-org/old"))
    if three_granted:
        grants.append(grant(7901, "tidewater-eap/saved"))
    response = _agent_editor_page(
        root="https://mcp.test",
        state="fixture",
        invitation_hash="fixture",
        secret="fixture",
        agent_name="Atlas",
        workspace="Fixture",
        platform="discord",
        grants=grants,
        installations=installations,
        snapshot="fixture",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/daimon/installations/new",
        github_user_id=55,
    )
    return inline_assets(response.body.decode())


def saved_page() -> str:
    grants = [
        grant(7801, "northwind-labs/saved", working=True),
        grant(7802, "northwind-labs/new"),
        grant(7901, "tidewater-eap/saved"),
        grant(7902, "tidewater-eap/new"),
    ]
    response = _saved_agent_page(
        "Atlas",
        grants,
        {repo.repo_id for repo in grants},
        installations=(
            _Installation(
                id=78,
                owner_id=56,
                owner_login="northwind-labs",
                repository_selection="selected",
                owner_type="Organization",
                avatar_url="https://avatars.githubusercontent.com/u/56",
                repos=(),
            ),
            _Installation(
                id=79,
                owner_id=57,
                owner_login="tidewater-eap",
                repository_selection="selected",
                owner_type="Organization",
                repos=(),
            ),
        ),
    )
    return inline_assets(response.body.decode())


ROW_MEASURE = """() => {
  const rect = el => el.getBoundingClientRect();
  const center = el => (rect(el).top + rect(el).bottom) / 2;
  const firstLine = el => {
    const range = document.createRange();
    range.selectNodeContents(el.firstChild || el);
    const box = [...range.getClientRects()].find(item => item.width > 0);
    return box ? (box.top + box.bottom) / 2 : center(el);
  };
  const rows = [...document.querySelectorAll(
    '.gh-agent-row:not([hidden]), .gh-picker-choice:not([hidden]), .gh-saved-row'
  )].filter(el => rect(el).width && !el.closest('template'));
  return rows.map(row => {
    const name = row.querySelector('.gh-row-name, .gh-picker-name, .gh-saved-name') || row;
    const icon = row.querySelector('.gh-row-icon .web-icon, .gh-row-icon');
    const action = row.querySelector('.gh-row-action:not([hidden]), input[type=checkbox]');
    const style = getComputedStyle(name);
    return {text: name.textContent.trim().slice(0, 30),
      cls: row.className + '/' + name.className, height: rect(row).height,
      hasNote: !!row.querySelector('.gh-repo-locked, .gh-repo-state'),
      lines: Math.round(rect(name).height / parseFloat(style.lineHeight)),
      iconDy: icon ? center(icon) - firstLine(name) : null,
      actionDy: action ? center(action) - firstLine(name) : null,
      weight: style.fontWeight, color: style.color, size: style.fontSize};
  });
}"""


def check_rows(page: Page) -> int:
    rows = page.evaluate(ROW_MEASURE)
    assert rows, "No rows measured"
    for row in rows:
        assert row["height"] >= 56, row
        if row["lines"] == 1 and not row["hasNote"]:
            assert abs(row["height"] - 56) <= 1, row
        elif row["lines"] > 1 and not row["hasNote"]:
            assert abs(row["height"] - (56 + (row["lines"] - 1) * 24)) <= 1, row
        for key in ("iconDy", "actionDy"):
            if row[key] is not None:
                assert abs(row[key]) <= 1, (key, row)
        assert row["weight"] == "400", row
        removed = "gh-removed-row" in row["cls"]
        assert row["color"] == (MUTED if removed else NAVY), row
        assert row["size"] == "16px", row
    anatomy = page.evaluate(
        """() => {
          const rect = el => el.getBoundingClientRect();
          const cx = el => (rect(el).left + rect(el).right) / 2;
          const cy = el => (rect(el).top + rect(el).bottom) / 2;
          const heads = [...document.querySelectorAll('.gh-owner-heading')]
            .filter(el => rect(el).width).map(head => {
              const avatar = head.querySelector('.gh-owner-avatar, .gh-owner-avatar-fallback');
              const icon = head.parentElement.querySelector('.gh-row-icon');
              return {height: rect(head).height,
                column: avatar && icon ? cx(avatar) - cx(icon) : null};
            });
          const card = document.querySelector('#working-card:not([hidden])');
          const name = card?.querySelector('#working-card-name');
          const button = card?.querySelector('#change-working');
          const pin = card?.querySelector('.web-icon');
          let cardData = null;
          if (card && name) {
            const lines = Math.round(rect(name).height / 24);
            const first = document.createRange();
            first.selectNodeContents(name.firstElementChild || name);
            const line = [...first.getClientRects()].find(r => r.width);
            const firstCy = innerWidth <= 600 ? cy(card.querySelector(':scope > span')) :
              line ? (line.top + line.bottom) / 2 : cy(name);
            cardData = {height: rect(card).height, lines,
              ownerLines: name.querySelector('.gh-working-owner') ?
                Math.round(rect(name.querySelector('.gh-working-owner')).height / 24) : null,
              iconDy: pin ? cy(pin) - firstCy : null,
              actionDy: button ? cy(button) - firstCy : null,
              ownerMuted: !!name.querySelector('.gh-working-owner') &&
                getComputedStyle(name.querySelector('.gh-working-owner')).color !==
                getComputedStyle(name).color,
              text: name.innerText};
          }
          return {heads, card: cardData};
        }"""
    )
    for head in anatomy["heads"]:
        assert head["height"] == 40, head
        if head["column"] is not None:
            assert abs(head["column"]) <= 1, head
    card = anatomy["card"]
    if card and "Hidden repo" not in card["text"]:
        expected_height = (80 if page.viewport_size["width"] <= 600 else 56) + (
            card["lines"] - 1
        ) * 24
        assert abs(card["height"] - expected_height) <= 2, card
        assert card["ownerLines"] == 1, card
        assert card["ownerMuted"] and "/" in card["text"], card
        assert abs(card["iconDy"]) <= 1, card
        assert abs(card["actionDy"]) <= 1, card
    return len(rows)


CONTRAST_JS = """(specs) => {
  const parse = value => {
    const m = value.match(/[\\d.]+/g).map(Number);
    return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1];
  };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3]));
  const background = el => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) {
      const color = parse(getComputedStyle(node).backgroundColor);
      if (color[3] > 0) layers.push(color);
      if (color[3] === 1) break;
    }
    return layers.reduceRight((under, layer) => over(layer, under), [255, 255, 255]);
  };
  const luminance = rgb => {
    const [r, g, b] = rgb.map(c => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  return specs.map(([label, selector, minimum, prop = "color"]) => {
    const el = [...document.querySelectorAll(selector)].find(item => item.getClientRects().length);
    if (!el) return {label, missing: true};
    const style = getComputedStyle(el);
    const bg = background(prop === "color" ? el : el.parentElement);
    const fg = over(parse(style[prop]), bg);
    return {label, minimum, ratio: Math.round(ratio(fg, bg) * 100) / 100};
  });
}"""

# (label, selector, minimum ratio): 4.5 for text, 3 for icons and meaningful borders.
EDITOR_CONTRAST = [
    ("row name", ".gh-agent-row:not(.gh-removed-row) .gh-row-name", 4.5),
    ("owner heading", ".gh-owner-heading", 4.5),
    ("row action", ".gh-agent-row:not(.gh-removed-row) .gh-remove-action", 4.5),
    ("working badge", ".gh-agent-row .gh-working-badge", 4.5),
    ("working card label", "#working-card > span", 4.5),
    ("working card pin", "#working-card > .web-icon", 3),
    ("row icon", ".gh-agent-row:not(.gh-removed-row) .gh-row-icon .web-icon", 3),
    ("dock to add", "#dock-add", 4.5),
    ("dock to remove", "#dock-remove", 4.5),
    ("dock working", "#dock-working", 4.5),
    ("added row label", ".gh-added-row .gh-pending-label", 4.5),
    ("removed row label", ".gh-removed-row .gh-pending-label", 4.5),
    ("removed row name", ".gh-removed-row .gh-row-name", 4.5),
    ("undo", ".gh-undo-action:not([hidden])", 4.5),
    ("save", "#save-repos", 4.5),
    ("add button border", "#open-repo-picker", 3, "borderTopColor"),
    ("working card stripe", "#working-card", 3, "borderLeftColor"),
]
DIALOG_CONTRAST = [
    ("sheet title", ".gh-repo-dialog[open] h2", 4.5),
    ("sheet chip", ".gh-repo-dialog[open] .gh-sheet-chip", 4.5),
    ("shown count", "#shown-count", 4.5),
    ("disabled add", "#stage-repos:disabled", 4.5),
    ("disabled add (owners)", "#owner-stage-repos:disabled", 4.5),
    ("added chip", ".gh-picker-connected small", 4.5),
    ("owner added chip", ".gh-owner-added", 4.5),
    ("checked row", ".gh-picker-choice:has(.gh-picker-toggle:checked) .gh-picker-name", 4.5),
    ("amber warning", ".gh-owner-warning", 4.5),
    ("amber warning icon", ".gh-owner-warning .web-icon", 3),
    ("try again", ".gh-owner-retry", 4.5),
    ("limit note", ".gh-owner-truncated:not([hidden])", 4.5),
    ("search icon", ".gh-dialog-search .web-icon", 3),
    ("not listed", ".gh-not-listed", 4.5),
    ("search border", "#search-repos", 3, "borderTopColor"),
    ("outline button border", ".gh-dialog-bulk #select-shown", 3, "borderTopColor"),
    ("try again border", ".gh-owner-retry", 3, "borderTopColor"),
]


RATIOS: dict[str, float] = {}


def check_contrast(page: Page, specs: list[tuple]) -> None:
    """Compute WCAG contrast of the rendered text or icon colour on its real backdrop."""
    for result in page.evaluate(CONTRAST_JS, [list(spec) for spec in specs]):
        if result.get("missing"):
            continue
        assert result["ratio"] >= result["minimum"], result
        RATIOS[result["label"]] = min(result["ratio"], RATIOS.get(result["label"], 99))


def computed(page: Page, selector: str, prop: str) -> str:
    return page.locator(selector).first.evaluate(f"el => getComputedStyle(el).{prop}")


def has_icon(page: Page, selector: str, name: str) -> bool:
    return page.locator(f"{selector} .web-icon--{name}").count() > 0


def check_backdrop(page: Page) -> None:
    """The dialog scrim must reach every corner of the visible viewport."""
    width, height = page.viewport_size["width"], page.viewport_size["height"]
    box = page.locator("dialog[open]").bounding_box()
    image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    points = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    points += [(width // 2, 10), (width // 2, height - 1), (3, height // 2)]
    for x, y in points:
        inside = (
            box["x"] <= x <= box["x"] + box["width"] and box["y"] <= y <= box["y"] + box["height"]
        )
        if not inside:
            assert max(image.getpixel((x, y))) < 200, ((x, y), image.getpixel((x, y)))


def check_roles(page: Page) -> None:
    """Colour roles with the icon or text that must accompany each one."""
    added = ".gh-agent-row.gh-added-row .gh-pending-label"
    assert computed(page, added, "color") == GREEN_FG, "to add: green"
    assert computed(page, added, "backgroundColor") == GREEN_BG
    assert "To add" in page.locator(added).first.inner_text()
    removed = ".gh-agent-row.gh-removed-row"
    assert computed(page, f"{removed} .gh-pending-label", "color") == RED_FG, "to remove: red"
    assert computed(page, f"{removed} .gh-pending-label", "backgroundColor") == RED_BG
    assert "line-through" in computed(page, f"{removed} .gh-row-name", "textDecorationLine")
    undo = f"{removed} .gh-undo-action"
    assert page.locator(undo).inner_text() == "Undo" and has_icon(page, undo, "undo-2")
    remove = ".gh-agent-row:not(.gh-removed-row) .gh-remove-action"
    assert page.locator(remove).first.inner_text() == "Remove"
    assert has_icon(page, remove, "trash-2")
    settle(page)  # Undo replaces the clicked Remove, so the pointer would hover it.
    for prop in ("color", "fontWeight", "fontSize"):
        assert computed(page, undo, prop) == computed(page, remove, prop), f"undo vs remove {prop}"
    check_chips_follow_name(page)
    badge = ".gh-agent-row .gh-working-badge"
    assert computed(page, badge, "backgroundColor") == TEAL_BG, "working: teal"
    assert computed(page, badge, "color") == TEAL_FG and has_icon(page, badge, "pin")
    card = "#working-card"
    assert computed(page, card, "borderLeftColor") == TEAL_LINE and has_icon(page, card, "pin")
    assert computed(page, "#save-repos", "backgroundColor") == "rgb(12, 31, 64)", "primary: navy"
    assert has_icon(page, "#open-repo-picker", "plus")
    assert page.locator(".gh-row-icon .web-icon--book-marked").count() > 0
    for chip, background, color in (
        ("#dock-add", GREEN_BG, GREEN_FG),
        ("#dock-remove", RED_BG, RED_FG),
        ("#dock-working", TEAL_BG, TEAL_FG),
    ):
        assert computed(page, chip, "backgroundColor") == background, chip
        assert computed(page, chip, "color") == color, chip
    check_contrast(page, EDITOR_CONTRAST)


def check_chips_follow_name(page: Page) -> None:
    """State chips sit right after the repo name, on its line, not beside the action."""
    gaps = page.evaluate(
        """() => [...document.querySelectorAll(
          '.gh-agent-row:not([hidden]) .gh-working-badge:not([hidden]), ' +
          '.gh-agent-row:not([hidden]) .gh-pending-label:not([hidden])'
        )].filter(chip => chip.getClientRects().length).map(chip => {
          const name = chip.closest('.gh-agent-row').querySelector('.gh-row-name');
          const [chipBox, nameBox] = [chip, name].map(el => el.getBoundingClientRect());
          return {gap: chipBox.left - nameBox.right,
            dy: (chipBox.top + chipBox.bottom) / 2 - (nameBox.top + nameBox.bottom) / 2};
        })"""
    )
    assert gaps, "no state chips to measure"
    for gap in gaps:
        assert 0 <= gap["gap"] <= 16 and abs(gap["dy"]) <= 2, gap


def check_dock(page: Page) -> None:
    """On a phone Save takes the width Discard leaves; on a desktop it keeps its own width."""
    box = lambda selector: page.locator(selector).bounding_box()  # noqa: E731
    inner, save = box(".gh-editor-footer-inner"), box("#save-repos")
    padding = page.locator(".gh-editor-footer-inner").evaluate(
        "el => parseFloat(getComputedStyle(el).paddingRight)"
    )
    if page.viewport_size["width"] <= 600:
        assert abs((save["x"] + save["width"]) - (inner["x"] + inner["width"] - padding)) <= 1, save
        discard = box("#discard-repos")
        assert abs(save["x"] - (discard["x"] + discard["width"] + 11)) <= 1, (save, discard)
    else:
        assert save["width"] < inner["width"] / 4, save


def check_saved_layout(page: Page) -> None:
    """Saved page: centred check, title and line with a full-width button on a phone."""
    width = page.viewport_size["width"]
    phone = width <= 600
    rect = lambda selector: page.locator(selector).first.bounding_box()  # noqa: E731
    centre = lambda box: box["x"] + box["width"] / 2  # noqa: E731
    line = ".gh-status-line"
    back, groups = rect(".gh-back-discord"), rect(".gh-saved-groups")
    if phone:
        for selector in (".gh-status-icon .web-icon", ".gh-flow--saved h1", line):
            assert abs(centre(rect(selector)) - groups["x"] - groups["width"] / 2) <= 1, selector
        assert abs(back["width"] - groups["width"]) <= 1 and abs(back["x"] - groups["x"]) <= 1
        assert computed(page, line, "fontWeight") == "400"
        assert computed(page, line, "color") == MUTED
        assert computed(page, ".gh-saved-name", "textAlign") in ("left", "start")
    else:
        assert back["width"] < groups["width"] / 2, back
        assert abs(rect(".gh-flow--saved h1")["x"] - groups["x"]) <= 1
        assert computed(page, line, "fontWeight") == "650"


def check_sheet_roles(page: Page) -> None:
    """Dialog states: disabled add, checked rows and the owner chooser."""
    disabled = page.locator("#stage-repos:disabled, #owner-stage-repos:disabled")
    if disabled.count():
        button = disabled.first
        assert button.evaluate("el => getComputedStyle(el).backgroundColor") == MUTED_BG
        assert button.evaluate("el => getComputedStyle(el).opacity") == "1"
        assert button.evaluate("el => getComputedStyle(el).cursor") == "not-allowed"
        assert button.inner_text().startswith("Add 0")
    if page.locator("#repo-owner-choice").is_visible():
        assert has_icon(page, ".gh-not-listed", "info")
    assert page.locator(".gh-dialog-close[aria-label]").first.is_visible()
    check_contrast(page, DIALOG_CONTRAST)
    check_backdrop(page)


def check_warning(page: Page) -> None:
    """Partial access reads as amber, with the triangle-alert icon and a text action."""
    warning = page.locator(".gh-owner-warning:visible").first
    assert warning.evaluate("el => getComputedStyle(el).color") == AMBER_FG
    assert warning.evaluate("el => getComputedStyle(el).backgroundColor") == AMBER_BG
    assert warning.locator(".web-icon--triangle-alert").count() == 1
    retry = page.locator(".gh-owner-retry:visible").first
    assert retry.inner_text() == "Try again"
    assert retry.bounding_box()["height"] >= 44
    assert page.locator(".gh-owner-button:disabled").first.inner_text().strip()
    check_backdrop(page)
    check_contrast(page, DIALOG_CONTRAST)


def check_stale_grant(page: Page) -> None:
    """A grant from a lost org keeps its lock icon and an amber text explanation."""
    row = page.locator('.gh-owner-group[data-owner="lost-org"] .gh-agent-row')
    assert row.locator(".web-icon--lock").count() == 1
    note = row.locator(".gh-repo-locked")
    assert note.inner_text() == "You can't see this repo on GitHub."
    assert note.locator(".web-icon--triangle-alert").count() == 1
    assert note.evaluate("el => getComputedStyle(el).color") == AMBER_FG
    assert note.evaluate("el => getComputedStyle(el).backgroundColor") == AMBER_BG
    check_contrast(page, [("stale note", ".gh-repo-locked", 4.5)])


def settle(page: Page) -> None:
    """Park the pointer so no hover colour leaks into a screenshot."""
    page.mouse.move(0, 0)
    page.evaluate("document.activeElement && document.activeElement.blur()")


def capture(width: int) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        page.set_content(fixture_page(three_granted=True))
        font_loaded = page.evaluate(
            "() => document.fonts.ready.then(() => document.fonts.check('16px Inter'))"
        )
        assert font_loaded

        def shot(name: str, *, full_page: bool = True) -> None:
            settle(page)
            page.screenshot(path=str(SHOTS / f"{name}-{width}.png"), full_page=full_page)

        assert page.locator("#connected-repos .gh-owner-group:visible").count() == 3
        check_rows(page)
        shot("editor")

        page.get_by_role("button", name="Add repos", exact=True).click()
        check_sheet_roles(page)
        shot("owner-choice", full_page=False)
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        check_rows(page)
        check_sheet_roles(page)
        shot("owner-sheet", full_page=False)
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.locator("#stage-repos").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 1
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.get_by_role("button", name="Back").click()
        assert page.locator("#owner-picker-count").inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        page.locator("#show-selected-repos").click()
        assert page.locator(".gh-selected-owner").count() == 2
        assert page.locator("#shown-count").inner_text() == "Showing 2 selected"
        assert page.locator("#show-selected-repos").inner_text() == "Selected (2)"
        assert page.locator("#show-selected-repos").get_attribute("aria-pressed") == "true"
        check_rows(page)
        check_sheet_roles(page)
        shot("selected", full_page=False)
        page.locator("#stage-repos").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 2
        page.locator('.gh-agent-row[data-repo-id="7701"] .gh-remove-action').click()
        assert page.locator("#selection-status").inner_text() == "Not saved yet"
        expect(page.locator('.gh-agent-row[data-repo-id="7701"] .gh-undo-action')).to_be_visible()
        check_rows(page)
        check_roles(page)
        check_dock(page)
        shot("unsaved", full_page=False)

        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 2
        assert sorted(
            page.locator("#github-connect-form").evaluate(
                "form => new FormData(form).getAll('repo')"
            )
        ) == ["7802", "7902"]
        assert page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).keys()].every(key => !key.startsWith('access_'))"
        )
        assert page.locator("#github-connect-form").evaluate(
            "form => new FormData(form).getAll('remove')"
        ) == ["7701"]

        def save(route: Route) -> None:
            route.fulfill(status=200, content_type="text/html", body=saved_page())

        page.route("https://mcp.test/oauth/github/confirm", save)
        page.locator("#save-repos").click()
        expect(page.get_by_text("Atlas can read and change these 4 repos.")).to_be_visible()
        assert page.locator(".gh-saved-groups .gh-owner-group").count() == 2
        assert page.locator(".gh-saved-groups .gh-owner-avatar").count() == 1
        assert page.locator(".gh-saved-groups .web-icon--building-2").count() == 1
        check_rows(page)
        assert has_icon(page, ".gh-status-icon", "circle-check")
        assert computed(page, ".gh-saved-row .gh-working-badge", "backgroundColor") == TEAL_BG
        assert has_icon(page, ".gh-saved-row .gh-working-badge", "pin")
        assert computed(page, ".gh-back-discord", "backgroundColor") == NAVY_FILL
        check_saved_layout(page)
        check_contrast(
            page,
            [
                ("saved name", ".gh-saved-name", 4.5),
                ("saved heading", ".gh-owner-heading", 4.5),
                ("saved badge", ".gh-working-badge", 4.5),
                ("saved check", ".gh-status-icon .web-icon", 3),
                ("saved note", ".gh-action-line", 4.5),
                ("back button", ".gh-back-discord", 4.5),
            ],
        )
        shot("saved")

        capture_warning_states(page, width, shot)
        browser.close()


def capture_warning_states(page: Page, width: int, shot) -> None:
    """Owner fetch error, truncated listing and stale grant, with the disabled add button."""
    page.set_content(fixture_page(error_owner=True, truncated=True, suspended=False))
    page.get_by_role("button", name="Add repos", exact=True).click()
    check_warning(page)
    shot("owner-error", full_page=False)
    page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
    assert page.locator(".gh-owner-truncated:visible .web-icon--triangle-alert").count() == 1
    check_sheet_roles(page)
    shot("sheet-limit", full_page=False)
    page.locator("#close-repo-picker").click()
    page.set_content(fixture_page(error_owner=True, stale=True, three_granted=True))
    check_stale_grant(page)
    assert page.locator(".gh-owner-heading span .web-icon--triangle-alert").count() == 1
    shot("stale-grant")
    page.set_content(fixture_page(suspended=True))
    page.get_by_role("button", name="Add repos", exact=True).click()
    assert page.locator(".gh-owner-warning:visible").first.inner_text() == "Paused on GitHub"
    check_backdrop(page)
    shot("owner-paused", full_page=False)


def check_staged_edits(width: int) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")

        def fresh() -> None:
            page.set_content(fixture_page())

        def add_northwind() -> None:
            page.get_by_role("button", name="Add repos", exact=True).click()
            page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
            page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
            page.locator("#stage-repos").click()
            assert page.locator('.gh-agent-row[data-repo-id="7802"].gh-added-row').count() == 1

        def posted_repos() -> list[str]:
            return page.locator("#github-connect-form").evaluate(
                "form => new FormData(form).getAll('repo')"
            )

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').uncheck()
        assert posted_repos() == []
        assert page.locator('.gh-agent-row[data-repo-id="7802"]').count() == 0
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#stage-repos").click()
        assert posted_repos() == ["7902"]

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator("#show-selected-repos").click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').uncheck()
        assert posted_repos() == []
        assert page.locator("#show-selected-repos").get_attribute("aria-pressed") == "false"
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator("#owner-clear-selection").click()
        assert posted_repos() == []
        assert page.locator("#selection-status").inner_text() == "No changes to save"
        assert page.locator(".gh-editor-footer").is_hidden()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator("#show-selected-repos").click()
        page.locator("#clear-selection").click()
        assert posted_repos() == []
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.locator("#discard-repos").click()
        assert posted_repos() == []
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.locator('.gh-agent-row[data-repo-id="7802"] .gh-remove-action').click()
        assert posted_repos() == []
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        browser.close()
    return 6


def check_no_js() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 390, "height": 900}, java_script_enabled=False)
        page.set_content(fixture_page(one_owner=True))
        choice = page.locator('noscript input[name="repo"]').first
        expect(choice).to_be_visible()
        choice.check()
        expect(page.get_by_role("button", name="Save changes")).to_be_visible()
        expect(page.get_by_role("button", name="Save changes")).to_be_enabled()
        browser.close()
    return 3


def check(width: int) -> int:
    html = fixture_page()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html)
        assert not errors, errors
        groups = page.locator("#connected-repos .gh-owner-group:visible")
        assert groups.count() == 2
        assert groups.nth(0).get_attribute("data-owner") == "northwind-labs"
        assert groups.nth(1).get_attribute("data-owner") == "sam-rivera"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        row_height = page.locator(".gh-agent-row:visible").first.bounding_box()["height"]
        assert row_height == 56 if width >= 768 else row_height >= 56
        avatar = page.locator(
            "#connected-repos .gh-owner-heading .gh-owner-avatar, "
            "#connected-repos .gh-owner-heading .gh-owner-avatar-fallback"
        ).first
        row_icon = page.locator("#connected-repos .gh-row-icon").first
        assert abs(avatar.bounding_box()["x"] - row_icon.bounding_box()["x"]) <= 1
        row_name = page.locator("#connected-repos .gh-row-name").first
        owner_name = page.locator("#connected-repos .gh-owner-heading").first
        owner_text_x = owner_name.evaluate(
            "el => { const range = document.createRange(); "
            "range.selectNodeContents(el.lastChild); return range.getBoundingClientRect().x; }"
        )
        assert abs(row_name.bounding_box()["x"] - owner_text_x) <= 1
        assert abs(row_name.bounding_box()["x"] - row_icon.bounding_box()["x"] - 32) <= 1
        if width == 390:
            wrap = page.locator("#working-card-name").evaluate(
                "el => { el.style.maxWidth = '130px'; "
                "const owner = el.querySelector('.gh-working-owner').getBoundingClientRect(); "
                "const repo = el.querySelector('.gh-working-repo').getBoundingClientRect(); "
                "return {ownerHeight: owner.height, repoBelow: repo.top >= owner.bottom}; }"
            )
            assert wrap == {"ownerHeight": 24, "repoBelow": True}, wrap
            page.locator("#working-card-name").evaluate(
                "el => el.style.removeProperty('max-width')"
            )
        assert (
            page.locator("#connected-repos .gh-owner-heading").first.bounding_box()["height"] == 40
        )
        check_rows(page)
        page.set_content(fixture_page(no_avatars=True))
        assert page.locator("#connected-repos .web-icon--user:visible").count() == 1
        assert page.locator("#connected-repos .web-icon--building-2:visible").count() == 1
        assert not page.locator("#connected-repos .gh-owner-avatar-fallback").first.inner_text()
        page.set_content(html)
        assert "northwind-labs/saved" in page.locator("#working-card").inner_text()
        page.locator("#change-working").click()
        page.locator("#working-choice").select_option("7701")
        page.locator("#confirm-working").click()
        assert "sam-rivera/saved" in page.locator("#working-card").inner_text()
        assert (
            page.locator("#connected-repos .gh-owner-group:visible").first.get_attribute(
                "data-owner"
            )
            == "sam-rivera"
        )

        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-choice").is_visible()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.get_by_role("button", name="Back").click()
        selected = page.locator('.gh-owner-button[data-owner="northwind-labs"] .gh-owner-selected')
        assert selected.inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        assert page.locator("#picker-count").inner_text() == "2 selected"
        page.get_by_role("button", name="Back").click()
        assert page.locator("#owner-picker-count").inner_text() == "2 selected"
        page.locator("#owner-stage-repos").click()
        assert page.locator("#repo-picker").is_hidden()
        assert page.locator("#connected-repos .gh-agent-row.gh-added-row").count() == 2
        assert page.locator("#connected-repos .gh-owner-group:visible").count() == 3
        posted = page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).getAll('repo')].sort()"
        )
        assert posted == ["7802", "7902"]
        assert page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).keys()].every(key => !key.startsWith('access_'))"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator('input[name="installation"]').count() == 0
        assert page.get_by_text("Remove all and switch").count() == 0

        page.set_content(fixture_page(one_owner=True))
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-sheet").is_visible()
        assert page.locator("#repo-owner-choice").is_hidden()
        page.locator("#close-repo-picker").click()

        page.set_content(fixture_page(error_owner=True, stale=True))
        assert page.locator('.gh-owner-group[data-owner="lost-org"]').is_visible()
        stale_row = page.locator('.gh-owner-group[data-owner="lost-org"] .gh-agent-row')
        assert stale_row.get_by_text("Hidden repo").count() == 1
        assert stale_row.locator(".gh-working-action").count() == 0
        stale_row.get_by_role("button", name="Remove Hidden repo").click()
        assert "9901" in page.locator("#github-connect-form").evaluate(
            "form => new FormData(form).getAll('remove')"
        )
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator('.gh-owner-button[data-owner="tidewater-eap"]').is_disabled()
        assert page.locator('.gh-owner-button[data-owner="northwind-labs"]').is_enabled()

        calls = 0

        def respond(route: Route) -> None:
            nonlocal calls
            calls += 1
            body = fixture_page(error_owner=calls == 1)
            route.fulfill(status=200, content_type="text/html", body=body)

        page.route("https://mcp.test/oauth/github/confirm?state=fixture", respond)
        page.goto("https://mcp.test/oauth/github/confirm?state=fixture")
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-choice").is_visible(), page.locator(
            "#repo-picker"
        ).inner_text()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-retry[data-owner="tidewater-eap"]').click()
        expect(page.locator('.gh-owner-button[data-owner="tidewater-eap"]')).to_be_enabled()
        assert page.locator("#owner-picker-count").inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        assert page.locator("#picker-count").inner_text() == "2 selected"
        browser.close()
    return 30


if __name__ == "__main__":
    count = (
        check(390)
        + check(1280)
        + check_staged_edits(390)
        + check_staged_edits(1280)
        + check_no_js()
    )
    capture(390)
    capture(1280)
    for label, ratio in sorted(RATIOS.items(), key=lambda item: item[1]):
        print(f"  contrast {ratio:5.2f}  {label}")
    print(f"{count} browser checks passed; {len(list(SHOTS.glob('*.png')))} screenshots in {SHOTS}")
