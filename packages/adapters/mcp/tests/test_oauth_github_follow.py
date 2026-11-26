"""The connect page's offer to follow a GitHub org: who sees it, and what saving it does."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.adapters.mcp import oauth_github
from daimon.adapters.mcp.oauth_github_follow import follow_offers_html
from daimon.core import github_connect_flow, github_connect_follow
from daimon.core.features.snapshot import CACHE
from daimon.core.github_connect_follow import confirmed_offer, follow_offers
from daimon.core.github_connect_views import (
    ConnectInstallation,
    ConnectRepo,
    FollowOffer,
    agent_editor_view,
)
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import FeatureTarget, Role
from daimon.core.stores.feature_values import get_feature_value
from daimon.core.stores.github_access import AgentRepo
from daimon.core.stores.github_follow_org import agent_follows
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The harness is defined next to the route tests.
from .test_oauth_github_permissions import WRITE_INSTALLATION, _Editor, _editor, _Org

# The suite stubs the module attribute for every test; these tests ask the real function.
_REAL_ORG_PROOF = github_connect_follow.org_proof
_REAL_ORG_ADMIN_OFFERS = github_connect_follow.org_admin_offers
_STYLES = Path(oauth_github.__file__).parent / "static" / "web.css"
_CHROME = Path(
    os.environ.get(
        "CHROME", Path.home() / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome"
    )
)


@pytest.fixture(autouse=True)
def _live_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform's own admin check is not what these tests are about."""
    monkeypatch.setattr(github_connect_flow, "attest_native_admin", AsyncMock(return_value=True))


def _installation(
    inst: int = 1,
    login: str = "acme",
    *,
    selection: str = "all",
    repos: int = 3,
    truncated: bool = False,
    state: str = "active",
    owner_type: str = "Organization",
) -> ConnectInstallation:
    return ConnectInstallation(
        id=inst,
        owner_id=inst,
        owner_login=login,
        repository_selection=selection,
        owner_type=owner_type,
        repos=tuple(
            ConnectRepo(inst * 100 + n, inst, inst, f"{login}/repo-{n}", "admin")
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
        _installation(owner_type="User"),
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
    view = agent_editor_view(
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
    return oauth_github._agent_editor_page(view).body.decode()


def test_the_editor_page_has_the_checkbox_only_when_offered() -> None:
    offer = FollowOffer(1, "acme", frozenset({100, 101, 102}))
    assert _page(()).count('name="follow_org"') == 0
    assert _page((offer,)).count('name="follow_org"') == 2, "the form, and the no-script form"
    note = _page((), following=True)
    assert "Atlas gets new repos added to its org on its own." in note


async def _follows(editor: _Editor) -> bool:
    CACHE.clear()
    async with editor.sessionmaker() as session:
        return await agent_follows(
            session,
            None,
            tenant_id=editor.tenant_id,
            platform=editor.platform,
            ma_agent_id="ag_editor",
        )


_ORG = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
_FOLLOW = {"access": "write", "repo": "101", "follow_org": "77"}


@pytest.mark.asyncio
async def test_an_admin_of_the_org_turns_it_on_and_the_gap_is_queued(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_connect_follow, "org_proof", AsyncMock(return_value="org_admin"))
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG]) as editor:
        hidden, page = await editor.page()
        assert 'name="follow_org" value="77"' in page, "the whole org is offered"

        saved = await editor.save(hidden, **_FOLLOW)

        assert saved.status_code == 200, saved.text
        assert await editor.grants() == {101: "write"}
        assert await _follows(editor)
        async with editor.sessionmaker() as session:
            row = await get_feature_value(
                session,
                tenant_id=editor.tenant_id,
                target=FeatureTarget("agent", agent_id="ag_editor"),
                key="github_follow_org",
            )
            job = await session.scalar(
                text(
                    "SELECT state FROM github_installation_reconciliations WHERE installation_id = 77"
                )
            )
        assert row is not None and row.set_by == editor.account_id and row.surface == "web"
        assert job == "pending", "the org is read now, not in an hour"
        assert "gets new repos added to its org on its own" in saved.text


@pytest.mark.asyncio
async def test_a_client_workspace_admin_who_is_not_an_org_admin_cannot_turn_it_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signer sees one repo of the org; the box is not offered and a forged post is refused."""
    checked = AsyncMock(return_value=None)
    monkeypatch.setattr(github_connect_follow, "org_proof", checked)
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG]) as editor:
        hidden, page = await editor.page()
        assert 'name="follow_org"' not in page
        assert checked.await_args is not None and checked.await_args.args[2].owner == "alpha"

        refused = await editor.save(hidden, **_FOLLOW)

        assert refused.status_code == 403
        assert "Only someone who sees every repo of alpha" in refused.text
        assert await editor.grants() == {}, "nothing was saved with it"
        assert not await _follows(editor)


@pytest.mark.asyncio
async def test_an_owner_who_is_no_longer_an_admin_cannot_turn_it_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored role still says admin; the live check does not."""
    monkeypatch.setattr(github_connect_follow, "org_proof", AsyncMock(return_value="org_admin"))
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG], requester="owner") as editor:
        async with editor.sessionmaker.begin() as session:
            await set_role(session, editor.account_id, Role.ADMIN)
        hidden, _ = await editor.page()
        live = AsyncMock(return_value=False)
        monkeypatch.setattr(github_connect_flow, "attest_native_admin", live)

        refused = await editor.save(hidden, **_FOLLOW)

        assert refused.status_code == 403
        live.assert_awaited()
        assert await editor.grants() == {}
        assert not await _follows(editor)


@pytest.mark.asyncio
async def test_an_owner_without_the_follow_box_still_saves_without_a_live_admin_check(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG], requester="owner") as editor:
        hidden, page = await editor.page()
        assert 'name="follow_org"' not in page, "an owner who is not an admin is not offered it"
        live = AsyncMock(return_value=False)
        monkeypatch.setattr(github_connect_flow, "attest_native_admin", live)

        saved = await editor.save(hidden, access="write", repo="101")

        assert saved.status_code == 200
        live.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_person_who_is_not_an_admin_gets_no_offer_and_no_follow(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_connect_follow, "org_proof", AsyncMock(return_value="org_admin"))
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG], requester="member") as editor:
        hidden, page = await editor.page()
        refused = await editor.save(hidden, **_FOLLOW)

        assert 'name="follow_org"' not in page
        assert refused.status_code in (400, 403)
        assert not await _follows(editor)


@pytest.mark.asyncio
async def test_ticking_it_without_the_whole_org_saves_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_connect_follow, "org_proof", AsyncMock(return_value="org_admin"))
    two = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin"), (102, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [two]) as editor:
        hidden, _ = await editor.page()

        refused = await editor.save(hidden, **_FOLLOW)

        assert refused.status_code == 400
        assert await editor.grants() == {}
        assert not await _follows(editor)


_OFFER = FollowOffer(77, "alpha", frozenset({101, 102}))


def _github_org(
    *,
    membership: tuple[int, dict[str, object]],
    installation_repos: list[int],
    total: int | None = None,
) -> httpx.AsyncClient:
    """GitHub: what it says of the signer's membership, and the App's whole listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/memberships/orgs/alpha":
            assert request.headers["authorization"] == "Bearer user-token"
            return httpx.Response(membership[0], json=membership[1])
        if path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "listing"})
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": len(installation_repos) if total is None else total,
                    "repositories": [
                        {"id": n, "full_name": f"alpha/repo-{n}", "owner": {"id": 55}}
                        for n in installation_repos
                    ],
                },
            )
        raise AssertionError(path)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_DENIED = (403, {"message": "Resource not accessible by integration"})
_ADMIN = (200, {"state": "active", "role": "admin"})


async def _proof(**github: Any) -> Any:
    async with _github_org(**github) as client:
        return await _REAL_ORG_PROOF(client, "user-token", _OFFER, app_jwt="app-jwt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership", "expected"),
    [
        (_ADMIN, "org_admin"),
        ((200, {"state": "active", "role": "member"}), None),
        ((200, {"state": "pending", "role": "admin"}), None),
    ],
)
async def test_github_saying_who_the_signer_is_settles_it(
    membership: tuple[int, dict[str, object]], expected: str | None
) -> None:
    assert await _proof(membership=membership, installation_repos=[101, 102, 103]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_a_refused_membership_falls_back_to_the_full_listing(status: int) -> None:
    refused = (status, {"message": "Resource not accessible by integration"})
    assert await _proof(membership=refused, installation_repos=[101, 102]) == "full_listing"


@pytest.mark.asyncio
async def test_a_refused_membership_with_only_part_of_the_org_visible_fails() -> None:
    """A client workspace's admin sees 2 of the org's 3 repos."""
    assert await _proof(membership=_DENIED, installation_repos=[101, 102, 103]) is None


@pytest.mark.asyncio
async def test_a_truncated_installation_listing_proves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="does not match total_count"):
        await _proof(membership=_DENIED, installation_repos=[101, 102], total=3)
    monkeypatch.setattr(github_connect_follow, "org_proof", _REAL_ORG_PROOF)  # the suite stubs it
    hidden = await _REAL_ORG_ADMIN_OFFERS(
        lambda: _github_org(membership=_DENIED, installation_repos=[101, 102], total=3),
        "user-token",
        [_OFFER],
        app_jwt="x",
    )
    assert hidden == (), "the offer is hidden"


@pytest.mark.asyncio
async def test_a_github_outage_is_not_read_as_a_no() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _proof(membership=(502, {}), installation_repos=[101, 102])


@pytest.mark.asyncio
async def test_the_save_records_which_proof_passed(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_connect_follow, "org_proof", AsyncMock(return_value="full_listing"))
    async with _editor(committing_sessionmaker, monkeypatch, [_ORG]) as editor:
        hidden, _ = await editor.page()

        saved = await editor.save(hidden, **_FOLLOW)

        assert saved.status_code == 200, saved.text
        async with editor.sessionmaker() as session:
            reasons = list(
                await session.scalars(
                    text(
                        "SELECT reason FROM security_audit_events "
                        "WHERE tenant_id = :t AND tool_name = 'github_connect'"
                    ),
                    {"t": editor.tenant_id},
                )
            )
        assert any("proved by full_listing" in reason for reason in reasons)


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
