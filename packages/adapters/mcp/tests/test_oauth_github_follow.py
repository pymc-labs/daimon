"""The connect page's offer to follow a GitHub org: who sees it, and what saving it does."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp import oauth_github
from daimon.adapters.mcp.oauth_github_follow import (
    FollowOffer,
    confirmed_offer,
    follow_offers,
    follow_offers_html,
)
from daimon.core.features.snapshot import CACHE
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import get_feature_value
from daimon.core.stores.github_access import AgentRepo
from daimon.core.stores.github_follow_org import agent_follows
from sqlalchemy import text

# The fixture is defined next to the route tests; importing it registers it here.
from .test_oauth_github_connect import _EditorEnv, editor_env  # noqa: F401

_STYLES = Path(oauth_github.__file__).parent / "static" / "web.css"
_CHROME = Path(
    os.environ.get(
        "CHROME", Path.home() / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome"
    )
)


@pytest.fixture(autouse=True)
def _live_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform's own admin check is not what these tests are about."""
    monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=True))


def _installation(
    inst: int = 1,
    login: str = "acme",
    *,
    selection: str = "all",
    repos: int = 3,
    truncated: bool = False,
    state: str = "active",
) -> oauth_github._Installation:
    return oauth_github._Installation(
        id=inst,
        owner_id=inst,
        owner_login=login,
        repository_selection=selection,
        owner_type="Organization",
        repos=tuple(
            oauth_github._Repo(inst * 100 + n, inst, inst, f"{login}/repo-{n}", "admin")
            for n in range(repos)
        ),
        truncated=truncated,
        state=state,  # type: ignore[arg-type]
    )


def _held(repo_id: int, installation_id: int) -> AgentRepo:
    now = datetime.now(UTC)
    return AgentRepo(
        repo_id=repo_id,
        full_name=f"acme/held-{repo_id}",
        scope="agent",
        max_access="write",
        baseline_access="write",
        ceiling_access="write",
        status="active",
        added_by_account_id=None,
        added_at=now,
        granted_by_account_id=None,
        granted_at=now,
        installation_id=installation_id,
    )


def test_the_offer_is_for_an_admin_and_a_whole_org_only() -> None:
    whole = _installation()
    offers = follow_offers([whole], [], may_follow=True, following=False)
    assert [o.owner for o in offers] == ["acme"]
    assert follow_offers([whole], [], may_follow=False, following=False) == ()
    assert follow_offers([whole], [], may_follow=True, following=True) == ()
    for refused in (
        _installation(selection="selected"),
        _installation(truncated=True),
        _installation(state="suspended"),
        _installation(repos=0),
    ):
        assert follow_offers([refused], [], may_follow=True, following=False) == ()


def test_an_agent_with_repos_in_another_org_is_not_offered_the_follow() -> None:
    offers = follow_offers(
        [_installation(1), _installation(2, "other")],
        [_held(5, 1)],
        may_follow=True,
        following=False,
    )
    assert [o.installation_id for o in offers] == [1]


def test_a_save_that_leaves_part_of_the_org_out_is_refused() -> None:
    offer = FollowOffer(1, "acme", frozenset({100, 101, 102}))
    whole = {100, 101, 102}
    assert confirmed_offer([offer], installation_id=1, held_after=whole, installations_after={1})
    assert not confirmed_offer(
        [offer], installation_id=1, held_after={100, 101}, installations_after={1}
    )
    assert not confirmed_offer(
        [offer], installation_id=1, held_after=whole, installations_after={1, 2}
    ), "a repo of another org"
    assert not confirmed_offer(
        [offer], installation_id=2, held_after=whole, installations_after={1}
    ), "an org that was not offered"


def test_the_checkbox_is_one_plain_words_row_that_starts_hidden() -> None:
    markup = follow_offers_html([FollowOffer(1, "acme", frozenset({100}))], agent_name="Atlas <b>")
    assert "Also give Atlas &lt;b&gt; new repos added to acme" in markup
    assert 'name="follow_org" value="1" disabled' in markup
    assert "hidden" in markup.split(">", 1)[0], "the page's script shows it"
    assert " · " not in markup


def _page(follow: tuple[FollowOffer, ...], *, following: bool = False) -> str:
    page = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="Atlas",
        workspace="Test",
        platform="discord",
        grants=[],
        installations=[_installation()],
        snapshot="snap",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/x/installations/new",
        github_user_id=1,
        follow=follow,
        following=following,
    )
    return page.body.decode()


def test_the_editor_page_has_the_checkbox_only_when_offered() -> None:
    offer = FollowOffer(1, "acme", frozenset({100, 101, 102}))
    assert _page(()).count('name="follow_org"') == 0
    assert _page((offer,)).count('name="follow_org"') == 2, "the form, and the no-script form"
    note = _page((), following=True)
    assert "Atlas gets new repos added to its org on its own." in note


@pytest.fixture
def all_repos_orgs(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub reports the installation as covering every repo of the org."""
    original = oauth_github._installations

    async def whole_org(*args: Any, **kwargs: Any) -> list[oauth_github._Installation]:
        found = await original(*args, **kwargs)
        return [replace(item, repository_selection="all") for item in found]

    monkeypatch.setattr(oauth_github, "_installations", whole_org)


async def _follows(env: _EditorEnv) -> bool:
    CACHE.clear()
    async with env.sessionmaker() as session:
        return await agent_follows(
            session, None, tenant_id=env.tenant_id, platform="discord", ma_agent_id="ag_editor"
        )


_WHOLE_ORG = {"access": "write", "working": "keep", "repo": ["101", "102", "103"]}


@pytest.mark.asyncio
async def test_ticking_it_with_the_whole_org_turns_the_feature_on_for_the_signing_admin(
    editor_env: _EditorEnv,  # noqa: F811
    all_repos_orgs: None,
) -> None:
    env = editor_env
    state, hidden = await env.open_editor()
    page = await env.browser.get("/oauth/github/confirm", params={"state": state})
    assert 'name="follow_org" value="77"' in page.text, "the whole org is offered"

    saved = await env.browser.post(
        "/oauth/github/confirm", data={**hidden, **_WHOLE_ORG, "follow_org": "77"}
    )

    assert saved.status_code == 200, saved.text
    assert await env.saved_repo_ids() == [101, 102, 103]
    assert await _follows(env)
    async with env.sessionmaker() as session:
        row = await get_feature_value(
            session,
            tenant_id=env.tenant_id,
            target=FeatureTarget("agent", agent_id="ag_editor"),
            key="github_follow_org",
        )
    assert row is not None and row.set_by is not None and row.surface == "web"
    assert "gets new repos added to its org on its own" in saved.text


@pytest.mark.asyncio
async def test_ticking_it_without_the_whole_org_saves_nothing(
    editor_env: _EditorEnv,  # noqa: F811
    all_repos_orgs: None,
) -> None:
    env = editor_env
    _, hidden = await env.open_editor()

    refused = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, **_WHOLE_ORG, "repo": ["101"], "follow_org": "77"},
    )

    assert refused.status_code == 400
    assert await env.saved_repo_ids() == []
    assert not await _follows(env)


@pytest.mark.asyncio
async def test_a_selected_repos_org_is_not_offered_or_accepted(
    editor_env: _EditorEnv,  # noqa: F811
) -> None:
    env = editor_env
    state, hidden = await env.open_editor()
    page = await env.browser.get("/oauth/github/confirm", params={"state": state})
    assert 'name="follow_org"' not in page.text

    refused = await env.browser.post(
        "/oauth/github/confirm", data={**hidden, **_WHOLE_ORG, "follow_org": "77"}
    )

    assert refused.status_code == 400
    assert not await _follows(env)


@pytest.mark.asyncio
async def test_a_person_who_is_not_an_admin_gets_no_offer_and_no_follow(
    editor_env: _EditorEnv,  # noqa: F811
    all_repos_orgs: None,
) -> None:
    env = editor_env
    state, hidden = await env.open_editor()
    async with env.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE accounts SET role = 'user' WHERE tenant_id = :t"), {"t": env.tenant_id}
        )

    page = await env.browser.get("/oauth/github/confirm", params={"state": state})
    refused = await env.browser.post(
        "/oauth/github/confirm", data={**hidden, **_WHOLE_ORG, "follow_org": "77"}
    )

    assert 'name="follow_org"' not in page.text
    assert refused.status_code in (400, 403)
    assert not await _follows(env)


@pytest.fixture
def phone() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    if not _CHROME.exists():
        pytest.skip("Chromium for Playwright is not installed")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(_CHROME), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 375, "height": 800})
        page.set_content(_page((FollowOffer(1, "acme", frozenset({100, 101, 102})),)))
        page.add_style_tag(content=_STYLES.read_text())
        yield page
        browser.close()


def test_on_a_phone_the_offer_appears_with_the_last_repo_and_fits_the_screen(phone: Any) -> None:
    offer = phone.locator(".gh-follow-org")
    assert not offer.is_visible(), "nothing is added yet"

    phone.click("#open-repo-picker")
    phone.click("#select-shown")
    phone.click("#stage-repos")
    assert offer.is_visible(), "every repo of acme is now on the agent"
    box = offer.bounding_box()
    assert box is not None and box["height"] >= 44 and box["width"] <= 375
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375, "no sideways scroll"

    offer.click()
    assert offer.locator("input").is_checked()
    assert phone.inner_text("#selection-status") == "Not saved yet"
    assert phone.is_visible("#save-repos")

    phone.click(".gh-remove-action >> nth=0")
    assert not offer.is_visible(), "a repo taken off leaves the org half covered"
    assert not offer.locator("input").is_checked()
