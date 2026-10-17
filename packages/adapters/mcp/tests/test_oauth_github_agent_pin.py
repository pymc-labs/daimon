"""The agent editor's installation pin: pinned fetch, stale pin, hints and the final-state check."""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from daimon.adapters.mcp import oauth_github
from daimon.adapters.mcp.oauth_github import build_oauth_github_routes
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_connect
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

_WRITE = {
    "metadata": "read",
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
}
_STALE_TITLE = "You can't add acme repos right now."
_STALE_ACTION = (
    "Ask whoever manages acme on GitHub to check your access and the Daimon app. "
    "You can still remove these repos."
)
_HIDDEN_ROW = "Hidden repo"
_HIDDEN_LINE = "You can't see this repo on GitHub."
_PAST_CAP = "Past the 2,000-repo limit, so you can only remove it here."
_OWNER_ALL = 'class="gh-link-button gh-owner-all"'


@dataclass
class _Org:
    login: str
    owner_id: int
    repos: dict[int, str]
    suspended: bool = False
    # Repo ids this person has no GitHub permission on (listed, but not usable).
    no_permission: set[int] = field(default_factory=set)
    # More repos than were ever named: ids 10_000.. generated on demand.
    bulk: int = 0
    # What `total_count` says; None means "the real count".
    total_count: int | None = None
    kind: str = "Organization"
    # None: GitHub's own avatar URL for the account.
    avatar: str | None = None


@dataclass
class _Env:
    browser: httpx.AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID
    orgs: dict[int, _Org]
    github_calls: list[str] = field(default_factory=list)
    listing_down: bool = False
    repos_down: bool = False
    details_down: bool = False
    repos_delay: float = 0.0

    async def grant(
        self,
        installation_id: int,
        repo_id: int,
        *,
        name: str | None = None,
        working: bool = False,
        agent_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        status: Literal["active", "suspended", "revoked"] = "active",
        shared: bool = False,
    ) -> None:
        """Seed a confirmed repo and an agent grant on it, bypassing every check."""
        org = self.orgs[installation_id]
        async with self.sessionmaker.begin() as session:
            await insert_repo_and_grant(
                session,
                tenant_id=tenant_id or self.tenant_id,
                agent_id=agent_id or self.agent_id,
                repo_id=repo_id,
                installation_id=installation_id,
                owner_id=org.owner_id,
                full_name=name or org.repos[repo_id],
                working=working,
                status=status,
                shared=shared,
            )

    async def start(self, *, agent: bool = True, requested_repo: str | None = None) -> str:
        """Mint a fresh invitation, sign in, and return the flow's state."""
        if not agent:
            return await self._sign_in(await self._mint(agent=False))
        return await self._sign_in(await self._mint(agent=True, requested_repo=requested_repo))

    async def _mint(self, *, agent: bool, requested_repo: str | None = None) -> str:
        async with self.sessionmaker.begin() as session:
            return await github_connect.mint_invitation(
                session,
                tenant_id=self.tenant_id,
                requester_account_id=self.account_id,
                requester_label="Admin",
                requester_platform_user_id="editor",
                agent_id=self.agent_id if agent else None,
                agent_name="Editor" if agent else None,
                agent_ma_id="ag_editor" if agent else None,
                agent_manager_verified=agent,
                operator_issued=False,
                origin_platform="discord",
                requested_repo=requested_repo,
            )

    async def _sign_in(self, invitation: str) -> str:
        start = await self.browser.get(f"/oauth/github/connect/{invitation}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        await self.browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        return state

    async def get(self, state: str, **params: str) -> httpx.Response:
        return await self.browser.get("/oauth/github/confirm", params={"state": state, **params})

    async def editor(
        self, *, requested_repo: str | None = None, **params: str
    ) -> tuple[str, httpx.Response, list[tuple[str, str]]]:
        """Open the editor; return the state, the page and its hidden fields."""
        state = await self.start(requested_repo=requested_repo)
        page = await self.get(state, **params)
        assert page.status_code == 200, page.text
        return state, page, _hidden_pairs(page.text)

    async def post(
        self,
        hidden: list[tuple[str, str]],
        *,
        remove: tuple[int, ...] = (),
        add: tuple[int, ...] = (),
        working: str = "keep",
        drop: tuple[str, ...] = (),
        extra: tuple[tuple[str, str], ...] = (),
        url: str = "/oauth/github/confirm",
    ) -> httpx.Response:
        pairs = [(name, value) for name, value in hidden if name not in drop]
        pairs += [("remove", str(repo_id)) for repo_id in remove]
        for repo_id in add:
            pairs += [("repo", str(repo_id)), (f"access_{repo_id}", "write")]
        pairs += [("working", working), *extra]
        return await self.browser.post(
            url,
            content=urlencode(pairs),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    async def grants(self) -> dict[int, bool]:
        """repo id -> is the working repo, for this agent."""
        async with self.sessionmaker() as session:
            rows = await github_access.list_agent_repos(
                session, tenant_id=self.tenant_id, agent_id=self.agent_id
            )
        return {row.repo_id: row.is_working_repo for row in rows}

    async def invitation_active(self, hidden: list[tuple[str, str]]) -> bool:
        invitation = dict(hidden)["invitation"]
        async with self.sessionmaker() as session:
            status, _ = await github_connect.invitation_status(session, invitation)
        return status == "active"

    def repo_calls(self) -> list[str]:
        return [call for call in self.github_calls if call.endswith("/repositories")]


async def insert_repo_and_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    installation_id: int,
    owner_id: int,
    full_name: str,
    working: bool = False,
    status: str = "active",
    shared: bool = False,
) -> None:
    """Write an agent-scoped confirmed repo and a grant on it, bypassing every check."""
    await session.execute(
        text(
            "INSERT INTO tenant_github_repos (tenant_id, repo_id, scope_agent_id, owner_id, "
            "installation_id, repo_full_name, max_access, authorized_by_github_user_id, "
            "status, version) VALUES (:tenant, :repo, :agent, :owner, :installation, :name, "
            "'write', 17, :status, 1)"
        ),
        {
            "tenant": tenant_id,
            "repo": repo_id,
            "agent": None if shared else agent_id,
            "owner": owner_id,
            "installation": installation_id,
            "name": full_name,
            "status": status,
        },
    )
    await session.execute(
        text(
            "INSERT INTO agent_github_grants (tenant_id, agent_id, repo_id, baseline_access, "
            "ceiling_access, is_working_repo, version) "
            "VALUES (:tenant, :agent, :repo, 'write', 'write', :working, 1)"
        ),
        {"tenant": tenant_id, "agent": agent_id, "repo": repo_id, "working": working},
    )


def _hidden_pairs(page_html: str) -> list[tuple[str, str]]:
    """The fields a scripting browser submits: noscript and template content is not sent."""
    page_html = re.sub(r"<noscript>.*?</noscript>", "", page_html, flags=re.S)
    return [
        (name, html.unescape(value))
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page_html
        )
    ]


def _text(response: httpx.Response) -> str:
    return html.unescape(response.text)


def _settings(key: str) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="client",
            client_secret=SecretStr("secret"),
        ),
    )


def _repo_rows(installation_id: int, org: _Org) -> list[dict[str, object]]:
    named = [
        {
            "id": repo_id,
            "owner": {"id": org.owner_id},
            "full_name": name,
            "permissions": {"admin": repo_id not in org.no_permission},
        }
        for repo_id, name in org.repos.items()
    ]
    bulk = [
        {
            "id": 10_000 + index,
            "owner": {"id": org.owner_id},
            "full_name": f"{org.login}/bulk-{index}",
            "permissions": {"admin": True},
        }
        for index in range(org.bulk)
    ]
    return named + bulk


@pytest_asyncio.fixture
async def env(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Env]:
    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_editor")
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="pin-workspace")
        account = await make_account(session, tenant=tenant, id=account_id)
        from daimon.core.stores.accounts import set_role

        await set_role(session, account_id, Role.ADMIN)
        await make_platform_principal(
            session, platform="discord", external_id="editor", tenant=tenant, account=account
        )
    orgs = {
        77: _Org("acme", 55, {101: "acme/repo-101", 102: "acme/repo-102", 103: "acme/repo-103"}),
        88: _Org("beta", 66, {201: "beta/repo-201", 202: "beta/repo-202"}),
        99: _Org("gamma", 77, {301: "gamma/repo-301"}),
    }
    holder: list[_Env] = []

    async def github_handler(request: httpx.Request) -> httpx.Response:
        current = holder[0]
        path = request.url.path
        if path.startswith(("/user/installations", "/app/installations")):
            current.github_calls.append(path)
        page = int(request.url.params.get("page", "1"))
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        if path == "/applications/client/token":
            return httpx.Response(204)
        if path == "/user":
            return httpx.Response(200, json={"id": 17})
        if path == "/user/installations":
            if current.listing_down:
                return httpx.Response(502)
            rows = [
                {
                    "id": installation_id,
                    "account": {
                        "id": org.owner_id,
                        "login": org.login,
                        "type": org.kind,
                        "avatar_url": org.avatar
                        or f"https://avatars.githubusercontent.com/u/{org.owner_id}?v=4",
                    },
                    "repository_selection": "selected",
                    "suspended_at": "2026-01-01T00:00:00Z" if org.suspended else None,
                }
                for installation_id, org in current.orgs.items()
            ]
            return httpx.Response(200, json={"installations": rows[(page - 1) * 100 : page * 100]})
        match = re.fullmatch(r"/user/installations/(\d+)/repositories", path)
        if match:
            if current.repos_delay:
                await asyncio.sleep(current.repos_delay)
            org = current.orgs.get(int(match.group(1)))
            if current.repos_down or org is None:
                return httpx.Response(502)
            rows = _repo_rows(int(match.group(1)), org)
            total = org.total_count if org.total_count is not None else len(rows)
            return httpx.Response(
                200,
                json={"total_count": total, "repositories": rows[(page - 1) * 100 : page * 100]},
            )
        if path == "/app/installation-requests":
            return httpx.Response(200, json={"installation_requests": []})
        match = re.fullmatch(r"/app/installations/(\d+)", path)
        if match:
            if current.details_down:
                return httpx.Response(502)
            org = current.orgs[int(match.group(1))]
            return httpx.Response(
                200,
                json={
                    "id": int(match.group(1)),
                    "account": {"id": org.owner_id, "login": org.login, "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": _WRITE,
                },
            )
        raise AssertionError(path)

    async def find_agent(_client: object, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> object:
        return SimpleNamespace(name="Editor", metadata={})

    monkeypatch.setattr(oauth_github, "find_agent_by_derived_uuid", find_agent)
    monkeypatch.setattr(oauth_github, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")
    monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=True))
    key = Fernet.generate_key().decode()
    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=_settings(key),
        sessionmaker=sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(github_handler)),
        deployment_default=DeploymentDefault(),
        anthropic=object(),  # type: ignore[arg-type]
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/connect/{token}", connect),
            Route("/oauth/github/callback", callback),
            Route("/oauth/github/setup", setup),
            Route("/oauth/github/confirm", confirm, methods=["GET", "POST"]),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        current = _Env(browser, sessionmaker, tenant_id, agent_id, account_id, orgs)
        holder.append(current)
        yield current


# --- Pinned fetch ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_editor_lists_one_installation_not_all(env: _Env) -> None:
    await env.grant(77, 101)
    _state, page, hidden = await env.editor()

    assert env.github_calls == ["/user/installations", "/user/installations/77/repositories"]
    assert "acme/repo-101" in page.text and "acme/repo-102" in page.text
    assert "beta/repo-201" not in page.text and "gamma/repo-301" not in page.text
    assert ("installation", "77") in hidden
    assert _STALE_TITLE not in _text(page)


@pytest.mark.asyncio
async def test_a_save_without_a_pin_or_hint_still_lists_every_installation(env: _Env) -> None:
    # Only a page stops at the account or org choice. A save from a form that predates it
    # (no `installation` field) is checked against every installation, as before.
    state = await env.start()
    page = await env.get(state, org="77")
    hidden = [pair for pair in _hidden_pairs(page.text) if pair[0] != "installation"]
    before = len(env.github_calls)

    saved = await env.post(hidden, add=(201,))

    assert saved.status_code == 200, saved.text
    assert env.github_calls[before : before + 4] == [
        "/user/installations",
        "/user/installations/77/repositories",
        "/user/installations/88/repositories",
        "/user/installations/99/repositories",
    ]
    assert await env.grants() == {201: False}


@pytest.mark.asyncio
async def test_pinned_editor_saves_within_the_pinned_installation(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()

    saved = await env.post(hidden, add=(102,), remove=(101,))

    assert saved.status_code == 200, saved.text
    assert set(await env.grants()) == {102}


@pytest.mark.asyncio
async def test_pinned_editor_refuses_a_repo_from_another_installation(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()

    refused = await env.post(hidden, add=(201,))

    assert refused.status_code == 403
    assert set(await env.grants()) == {101}
    assert await env.invitation_active(hidden)


# --- The untrusted installation hints --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        [("org", "abc")],
        [("org", "")],
        [("org", "-1")],
        [("org", "0")],
        [("org", "1e3")],
        [("org", "٣")],
        [("org", "9" * 25)],
        [("org", "77"), ("org", "77")],
    ],
)
async def test_malformed_or_duplicate_org_is_refused_before_github(
    env: _Env, query: list[tuple[str, str]]
) -> None:
    await env.grant(77, 101)
    state = await env.start()
    before = len(env.github_calls)

    page = await env.browser.get("/oauth/github/confirm", params=[("state", state), *query])

    assert page.status_code == 400
    assert env.github_calls[before:] == []


@pytest.mark.asyncio
async def test_org_for_another_installation_opens_a_switch_page(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    state = await env.start()
    before = len(env.github_calls)

    page = await env.get(state, org="88")

    assert page.status_code == 200
    # The destination to add from, then the source so its rows can show names.
    assert env.github_calls[before:] == [
        "/user/installations",
        "/user/installations/88/repositories",
        "/user/installations/77/repositories",
    ]
    assert ("installation", "88") in _hidden_pairs(page.text)
    assert 'data-name="beta/repo-201"' in page.text  # an add option
    assert 'data-name="acme/repo-103"' not in page.text  # source repos are not add options
    assert page.text.count('name="remove"') >= 2 and "acme/repo-101" in page.text
    assert not re.search(r'id="open-repo-picker"[^>]*\sdisabled', page.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("org", ["12345", "88"])
async def test_switch_destination_must_be_a_live_installation_in_the_list(
    env: _Env, org: str
) -> None:
    await env.grant(77, 101)
    env.orgs[88].suspended = org == "88"
    state = await env.start()

    page = await env.get(state, org=org)

    assert page.status_code == 403
    assert env.repo_calls() == []


@pytest.mark.asyncio
async def test_org_matching_the_pin_is_accepted(env: _Env) -> None:
    await env.grant(77, 101)
    state = await env.start()

    page = await env.get(state, org="77")

    assert page.status_code == 200
    assert ("installation", "77") in _hidden_pairs(page.text)


@pytest.mark.asyncio
async def test_forged_installation_field_is_refused_on_save(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor()

    for forged in (
        (("installation", "88"),),
        (("installation", "77"), ("installation", "77")),
        (("installation", "abc"),),
        (("installation", "77"), ("installation", "88")),
    ):
        response = await env.post(hidden, remove=(101,), drop=("installation",), extra=forged)
        assert response.status_code in (400, 403), forged
        assert set(await env.grants()) == {101, 102}
        assert await env.invitation_active(hidden)

    # A conflicting org in the URL is refused the same way.
    response = await env.post(hidden, remove=(101,), url="/oauth/github/confirm?org=88")
    assert response.status_code == 400
    assert set(await env.grants()) == {101, 102}


@pytest.mark.asyncio
async def test_installation_field_matching_the_pin_saves(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()
    assert ("installation", "77") in hidden

    saved = await env.post(hidden, remove=(101,))

    assert saved.status_code == 200 and await env.grants() == {}


@pytest.mark.asyncio
async def test_without_a_pin_org_narrows_the_listing_to_a_verified_installation(
    env: _Env,
) -> None:
    state = await env.start()
    before = len(env.github_calls)

    page = await env.get(state, org="88")

    assert page.status_code == 200
    assert env.github_calls[before:] == [
        "/user/installations",
        "/user/installations/88/repositories",
    ]
    assert "beta/repo-201" in page.text and "acme/repo-101" not in page.text
    assert ("installation", "88") in _hidden_pairs(page.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("org", ["12345", "77"])
async def test_without_a_pin_org_must_be_a_live_installation_in_the_list(
    env: _Env, org: str
) -> None:
    env.orgs[77].suspended = org == "77"  # 12345 is not listed at all
    state = await env.start()

    page = await env.get(state, org=org)

    assert page.status_code == 403
    assert env.repo_calls() == []


@pytest.mark.asyncio
async def test_connect_page_without_an_agent_ignores_the_hints(env: _Env) -> None:
    await env.grant(77, 101)
    state = await env.start(agent=False)

    page = await env.get(state, org="abc")

    assert page.status_code == 200
    assert "repo-101" in page.text and "repo-201" in page.text
    assert len(env.repo_calls()) == 3  # every installation, as before


# --- Stale pin -------------------------------------------------------------------


def _make_stale(env: _Env, how: str) -> None:
    if how == "suspended":
        env.orgs[77].suspended = True
    elif how == "not_in_list":
        del env.orgs[77]
    elif how == "alone_and_gone":
        env.orgs.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["suspended", "not_in_list", "alone_and_gone"])
async def test_stale_pin_shows_rows_you_can_remove_but_not_add(env: _Env, how: str) -> None:
    await env.grant(77, 101, name="acme/secret-one", working=True)
    await env.grant(77, 102, name="acme/secret-two")
    _make_stale(env, how)

    _state, page, hidden = await env.editor()

    text = _text(page)
    assert _STALE_TITLE in text and _STALE_ACTION in text
    assert "gh-editor-notice-warn" in page.text
    assert "acme/secret" not in page.text  # names stay hidden
    assert page.text.count(_HIDDEN_ROW) == 2
    assert text.count(_HIDDEN_LINE) == 2
    assert "gh-editor-install" not in page.text
    assert re.search(r'id="open-repo-picker"[^>]*\sdisabled', page.text)
    assert "gh-working-action" not in page.text.split('<template id="added-row-template">')[0]
    assert page.text.count('name="remove"') >= 2
    assert ("installation", "77") in hidden
    assert env.repo_calls() == []  # no repo listing for a pin that cannot be used


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["suspended", "not_in_list"])
async def test_stale_pin_allows_removal_and_refuses_adds(env: _Env, how: str) -> None:
    await env.grant(77, 101, name="acme/secret-one", working=True)
    await env.grant(77, 102, name="acme/secret-two")
    _make_stale(env, how)
    _state, _page, hidden = await env.editor()

    added = await env.post(hidden, add=(103,))
    assert added.status_code == 403
    assert _STALE_TITLE in _text(added) and _STALE_ACTION in _text(added)
    added_with_removal = await env.post(hidden, add=(103,), remove=(102,))
    assert added_with_removal.status_code == 403
    assert set(await env.grants()) == {101, 102}
    assert await env.invitation_active(hidden)

    removed = await env.post(hidden, remove=(102,))
    assert removed.status_code == 200, removed.text
    assert await env.grants() == {101: True}


@pytest.mark.asyncio
async def test_stale_pin_removal_can_empty_the_agent(env: _Env) -> None:
    await env.grant(77, 101, name="acme/secret-one", working=True)
    await env.grant(77, 102, name="acme/secret-two")
    env.orgs[77].suspended = True
    _state, _page, hidden = await env.editor()

    removed = await env.post(hidden, remove=(101, 102))

    assert removed.status_code == 200, removed.text
    assert await env.grants() == {}
    assert not await env.invitation_active(hidden)


@pytest.mark.asyncio
async def test_stale_pin_working_repo_clears_only_with_its_own_removal(env: _Env) -> None:
    await env.grant(77, 101, name="acme/secret-one", working=True)
    await env.grant(77, 102, name="acme/secret-two")
    env.orgs[77].suspended = True
    _state, _page, hidden = await env.editor()

    # No other change to a repo that stays.
    for choice in ("clear", "102", "999"):
        refused = await env.post(hidden, remove=(102,), working=choice)
        assert refused.status_code == 403, choice
    only_clear = await env.post(hidden, working="clear")
    assert only_clear.status_code == 403
    assert await env.grants() == {101: True, 102: False}

    cleared = await env.post(hidden, remove=(101,), working="clear")
    assert cleared.status_code == 200, cleared.text
    assert await env.grants() == {102: False}


@pytest.mark.asyncio
async def test_removing_the_working_repo_clears_it_on_a_healthy_pin(env: _Env) -> None:
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor()

    removed = await env.post(hidden, remove=(101,))

    assert removed.status_code == 200
    assert await env.grants() == {102: False}


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_forged_removals_are_refused(env: _Env, stale: bool) -> None:
    other_agent = uuid.uuid4()
    other_tenant = uuid.uuid4()
    async with env.sessionmaker.begin() as session:
        await make_tenant(session, id=other_tenant, workspace_id="other-workspace")
    await env.grant(77, 101, working=True)
    await env.grant(77, 102, agent_id=other_agent)  # another agent's repo
    await env.grant(77, 103, tenant_id=other_tenant, agent_id=other_agent)  # another tenant's
    if stale:
        env.orgs[77].suspended = True
    _state, _page, hidden = await env.editor()
    before = await env.grants()

    for forged in (102, 103, 999, 201, 301):  # other agent, other tenant, unknown, unrelated
        refused = await env.post(hidden, remove=(forged,))
        assert refused.status_code == 403, forged
        mixed = await env.post(hidden, remove=(101, forged))
        assert mixed.status_code == 403, forged
    assert await env.grants() == before
    assert await env.invitation_active(hidden)
    async with env.sessionmaker() as session:
        other = await session.scalar(
            text("SELECT count(*) FROM agent_github_grants WHERE agent_id = :agent"),
            {"agent": other_agent},
        )
        assert other == 2


@pytest.mark.asyncio
async def test_stale_pin_removal_still_needs_the_live_admin_check(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.grant(77, 101)
    env.orgs[77].suspended = True
    _state, _page, hidden = await env.editor()

    monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=False))
    refused = await env.post(hidden, remove=(101,))
    assert refused.status_code == 403
    assert await env.grants() == {101: False}

    from daimon.core.native_admins import AdminAttestationUnavailable

    monkeypatch.setattr(
        oauth_github,
        "_attest_native_admin",
        AsyncMock(side_effect=AdminAttestationUnavailable("x")),
    )
    unavailable = await env.post(hidden, remove=(101,))
    assert unavailable.status_code == 503
    assert await env.grants() == {101: False}
    assert ("installation", "77") in _hidden_pairs(unavailable.text)  # the retry form keeps it


@pytest.mark.asyncio
async def test_stale_pin_removal_rejects_a_snapshot_that_moved(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    env.orgs[77].suspended = True
    _state, _page, hidden = await env.editor()
    async with env.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE agent_github_grants SET version = version + 1 "
                "WHERE agent_id = :agent AND repo_id = 102"
            ),
            {"agent": env.agent_id},
        )

    moved = await env.post(hidden, remove=(101,))

    assert moved.status_code == 409
    assert set(await env.grants()) == {101, 102}


# --- A slow or failing GitHub is not a stale pin ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["listing_down", "repos_down"])
async def test_github_failure_is_transient_not_stale(env: _Env, failure: str) -> None:
    await env.grant(77, 101, name="acme/secret-one")
    state = await env.start()
    setattr(env, failure, True)

    page = await env.get(state)

    assert page.status_code == 502
    assert "Couldn't reach GitHub" in _text(page)
    assert _STALE_TITLE not in _text(page) and "gh-editor-notice-warn" not in page.text
    assert "acme/secret" not in page.text

    setattr(env, failure, False)
    _state, _page, hidden = await env.editor()
    setattr(env, failure, True)
    failed = await env.post(hidden, remove=(101,))
    assert failed.status_code == 502
    assert ("installation", "77") in _hidden_pairs(failed.text)
    assert await env.grants() == {101: False}
    assert await env.invitation_active(hidden)


@pytest.mark.asyncio
async def test_a_timeout_on_the_pinned_listing_is_transient_not_stale(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.grant(77, 101)
    state = await env.start()
    monkeypatch.setattr(oauth_github, "_INSTALLATIONS_DEADLINE_SECONDS", 0.05)
    env.repos_delay = 30

    page = await env.get(state)

    assert page.status_code == 502
    assert _STALE_TITLE not in _text(page)


# --- A setup from before the one-installation rule --------------------------------


@pytest.mark.asyncio
async def test_legacy_multi_installation_agent_gets_an_error_page_and_no_listing(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    await env.grant(88, 201)
    state = await env.start()

    page = await env.get(state)

    assert page.status_code == 409
    text = _text(page)
    assert "Editor has repos from more than one GitHub account or org." in text
    assert "Remove the extra ones in chat, then reload this page." in text
    assert "<form" not in page.text and "acme/repo" not in text
    assert env.github_calls == []
    assert await env.grants() == {101: False, 201: False}


@pytest.mark.asyncio
async def test_legacy_multi_installation_save_changes_nothing(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()
    await env.grant(88, 201)  # a second installation appears after the page rendered
    calls = len(env.github_calls)

    refused = await env.post(hidden, remove=(101,))

    assert refused.status_code == 409
    assert "more than one GitHub account or org" in _text(refused)
    assert await env.grants() == {101: False, 201: False}
    assert await env.invitation_active(hidden)
    assert env.github_calls[calls:] == []


@pytest.mark.asyncio
async def test_pin_that_moves_between_the_two_reads_asks_for_a_reload(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await env.start()
    original = oauth_github.list_agent_repos
    reads = 0

    async def list_then_grant(
        session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> list[github_access.AgentRepo]:
        nonlocal reads
        rows = await original(session, tenant_id=tenant_id, agent_id=agent_id)
        reads += 1
        if reads == 1:
            await env.grant(88, 201)
        return rows

    monkeypatch.setattr(oauth_github, "list_agent_repos", list_then_grant)
    page = await env.get(state)

    assert page.status_code == 409
    assert "The agent's repos changed" in _text(page)


# --- The 2,000-repo cap -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_list_cut_at_the_cap_says_so_and_still_saves_inside_it(env: _Env) -> None:
    env.orgs[77].bulk = 2_100
    await env.grant(77, 101)
    _state, page, hidden = await env.editor()

    assert env.github_calls == [
        "/user/installations",
        *["/user/installations/77/repositories"] * 20,
    ]
    assert page.text.count("This list is limited to 2,000 repos.") == 2  # dialog and noscript
    assert _OWNER_ALL not in page.text
    assert "acme/bulk-1" in page.text and "acme/bulk-2100" not in page.text
    saved = await env.post(hidden, add=(10_005,))
    assert saved.status_code == 200, saved.text
    assert set(await env.grants()) == {101, 10_005}


@pytest.mark.asyncio
async def test_a_grant_beyond_the_cap_can_be_removed_but_not_changed(env: _Env) -> None:
    env.orgs[77].bulk = 2_100
    await env.grant(77, 101, name="acme/past-the-cap", working=True)
    await env.grant(77, 10_005, name="acme/bulk-5")  # inside the list
    # 101 is not among the generated repos, so it is not in the listing at all.
    del env.orgs[77].repos[101]
    del env.orgs[77].repos[102]
    del env.orgs[77].repos[103]
    _state, page, hidden = await env.editor()

    assert _PAST_CAP in _text(page)
    assert "past-the-cap" not in page.text
    # Its access and working-repo status need a listing it is not in.
    assert (await env.post(hidden, add=(101,))).status_code == 403
    assert (await env.post(hidden, working="101")).status_code == 403
    assert (await env.post(hidden, working="clear")).status_code == 403
    assert await env.grants() == {101: True, 10_005: False}

    removed = await env.post(hidden, remove=(101,))

    assert removed.status_code == 200, removed.text
    assert await env.grants() == {10_005: False}


@pytest.mark.asyncio
async def test_an_exactly_full_list_is_not_called_limited(env: _Env) -> None:
    env.orgs[77].repos = {}
    env.orgs[77].bulk = 2_000
    await env.grant(77, 101, name="acme/one")
    _state, page, _hidden = await env.editor()

    assert len(env.repo_calls()) == 20
    assert "This list is limited" not in page.text


@pytest.mark.asyncio
async def test_a_short_list_is_not_called_limited(env: _Env) -> None:
    await env.grant(77, 101)
    _state, page, _hidden = await env.editor()

    assert "This list is limited" not in page.text
    assert _OWNER_ALL in page.text


# --- The retry form keeps the installation ------------------------------------------


@pytest.mark.asyncio
async def test_failed_save_retry_form_carries_the_installation_and_resubmits(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()
    env.details_down = True

    failed = await env.post(hidden, add=(102,))

    assert failed.status_code == 502
    retry = _hidden_pairs(failed.text)
    assert ("installation", "77") in retry
    env.details_down = False
    again = await env.browser.post(
        "/oauth/github/confirm",
        content=urlencode(retry),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert again.status_code == 200, again.text
    assert set(await env.grants()) == {101, 102}


def test_installation_is_a_retry_field() -> None:
    assert "installation" in oauth_github._RETRY_FIELD_NAMES
    assert 'name="installation" value="77"' in oauth_github._retry_hidden_inputs(
        {"installation": ["77"], "unknown": ["x"]}
    )


# --- The final-state check --------------------------------------------------------


@pytest.mark.asyncio
async def test_final_state_with_two_installations_rolls_everything_back(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.grant(77, 101)
    await env.grant(77, 103)
    _state, _page, hidden = await env.editor()
    original = github_connect.activate_confirmed_agent

    async def stage_then_leak(session: AsyncSession, **kwargs: object) -> object:
        result = await original(session, **kwargs)  # type: ignore[arg-type]
        # Something else writes a grant on another installation without the checks.
        await insert_repo_and_grant(
            session,
            tenant_id=env.tenant_id,
            agent_id=env.agent_id,
            repo_id=201,
            installation_id=88,
            owner_id=66,
            full_name="beta/repo-201",
        )
        return result

    monkeypatch.setattr(github_connect, "activate_confirmed_agent", stage_then_leak)
    failed = await env.post(hidden, add=(102,), remove=(101,))

    assert failed.status_code == 400
    assert "more than one GitHub account or org" in _text(failed)
    assert await env.grants() == {101: False, 103: False}  # removal and staging undone
    assert await env.invitation_active(hidden)  # the invitation was not spent


@pytest.mark.asyncio
async def test_final_state_may_be_empty(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor()

    removed = await env.post(hidden, remove=(101,))

    assert removed.status_code == 200
    assert await env.grants() == {}


@pytest.mark.asyncio
async def test_an_installation_change_that_skips_the_snapshot_is_caught_in_the_transaction(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor()

    async def move_installation(**_kwargs: object) -> bool:
        # After the request's own reads and before its transaction: the snapshot
        # does not include installation ids, so only the pin check can see this.
        async with env.sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE tenant_github_repos SET installation_id = 88 WHERE repo_id = 102")
            )
        return True

    monkeypatch.setattr(oauth_github, "_attest_native_admin", move_installation)
    response = await env.post(hidden, remove=(101,))

    assert response.status_code == 409
    assert set(await env.grants()) == {101, 102}
    assert await env.invitation_active(hidden)


# --- Moving the agent to another installation in one save ----------------------------


async def _installation_ids(env: _Env) -> set[int]:
    async with env.sessionmaker() as session:
        pin = await github_access.agent_installation_pin(
            session, tenant_id=env.tenant_id, agent_id=env.agent_id
        )
    return set(pin.installation_ids)


@pytest.mark.asyncio
async def test_healthy_switch_removes_every_source_grant_and_adds_destination_repos(
    env: _Env,
) -> None:
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor(org="88")

    saved = await env.post(hidden, remove=(101, 102), add=(202, 201))

    assert saved.status_code == 200, saved.text
    # The first repo added becomes the working repo; the removed one is gone.
    assert await env.grants() == {202: True, 201: False}
    assert await _installation_ids(env) == {88}
    assert not await env.invitation_active(hidden)


@pytest.mark.asyncio
async def test_switch_working_repo_can_be_another_added_repo_or_none(env: _Env) -> None:
    await env.grant(77, 101, working=True)
    _state, _page, hidden = await env.editor(org="88")
    saved = await env.post(hidden, remove=(101,), add=(201, 202), working="202")
    assert saved.status_code == 200, saved.text
    assert await env.grants() == {201: False, 202: True}

    await env.grant(77, 103, working=True)  # a second agent state: back onto acme
    async with env.sessionmaker.begin() as session:
        await session.execute(
            text("DELETE FROM agent_github_grants WHERE agent_id = :a AND repo_id IN (201, 202)"),
            {"a": env.agent_id},
        )
    _state, _page, hidden = await env.editor(org="88")
    cleared = await env.post(hidden, remove=(103,), add=(201,), working="clear")
    assert cleared.status_code == 200, cleared.text
    assert await env.grants() == {201: False}


@pytest.mark.asyncio
async def test_stale_switch_removes_the_unusable_source_and_adds_destination_repos(
    env: _Env,
) -> None:
    await env.grant(77, 101, name="acme/secret-one", working=True)
    await env.grant(77, 102, name="acme/secret-two")
    env.orgs[77].suspended = True
    _state, page, hidden = await env.editor(org="88")

    assert _STALE_TITLE in _text(page) and _HIDDEN_ROW in page.text
    assert "acme/secret" not in page.text
    assert not re.search(r'id="open-repo-picker"[^>]*\sdisabled', page.text)
    saved = await env.post(hidden, remove=(101, 102), add=(201,))

    assert saved.status_code == 200, saved.text
    assert await env.grants() == {201: True}
    assert await _installation_ids(env) == {88}


@pytest.mark.asyncio
async def test_switch_that_leaves_a_source_grant_is_refused(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor(org="88")

    for removal in ((101,), ()):
        refused = await env.post(hidden, remove=removal, add=(201,))
        assert refused.status_code == 403, removal
    only_working = await env.post(hidden, working="clear")
    assert only_working.status_code == 403
    assert await env.grants() == {101: False, 102: False}
    assert await env.invitation_active(hidden)
    assert await _installation_ids(env) == {77}


@pytest.mark.asyncio
async def test_switch_counts_revoked_and_shared_grants_as_source_grants(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102, status="revoked")
    await env.grant(77, 103, shared=True)
    _state, _page, hidden = await env.editor(org="88")

    refused = await env.post(hidden, remove=(101, 102), add=(201,))
    assert refused.status_code == 403
    assert await _installation_ids(env) == {77}

    saved = await env.post(hidden, remove=(101, 102, 103), add=(201,))
    assert saved.status_code == 200, saved.text
    assert await env.grants() == {201: True}


@pytest.mark.asyncio
async def test_switch_adds_only_from_the_verified_destination(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor(org="88")

    for add in ((301,), (102,), (201, 301), (999,)):  # gamma, source repo, mixed, unknown
        refused = await env.post(hidden, remove=(101,), add=add)
        assert refused.status_code == 403, add
    assert await env.grants() == {101: False}
    assert await env.invitation_active(hidden)

    # Posting a destination that was never verified is refused too.
    gone = await env.post(
        hidden,
        remove=(101,),
        add=(301,),
        drop=("installation",),
        extra=(("installation", "12345"),),
    )
    assert gone.status_code == 403
    # A real installation in the person's list is a verified destination for its own repos only.
    other = await env.post(
        hidden,
        remove=(101,),
        add=(201,),
        drop=("installation",),
        extra=(("installation", "99"),),
    )
    assert other.status_code == 403
    assert await env.grants() == {101: False}


@pytest.mark.asyncio
async def test_switch_without_the_hint_cannot_reach_the_destination(env: _Env) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor(org="88")

    refused = await env.post(hidden, remove=(101,), add=(201,), drop=("installation",))

    assert refused.status_code == 403  # only the source is listed without the hint
    assert await env.grants() == {101: False}


@pytest.mark.asyncio
async def test_switch_removes_a_source_grant_past_the_cap(env: _Env) -> None:
    env.orgs[77].bulk = 2_100
    await env.grant(77, 101, name="acme/past-the-cap", working=True)
    await env.grant(77, 10_005, name="acme/bulk-5")
    del env.orgs[77].repos[101]
    _state, page, hidden = await env.editor(org="88")
    assert _PAST_CAP in _text(page)

    saved = await env.post(hidden, remove=(101, 10_005), add=(201,))

    assert saved.status_code == 200, saved.text
    assert await env.grants() == {201: True}


@pytest.mark.asyncio
async def test_switch_that_ends_on_two_installations_rolls_back(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.grant(77, 101)
    _state, _page, hidden = await env.editor(org="88")
    original = github_connect.activate_confirmed_agent

    async def stage_then_leak(session: AsyncSession, **kwargs: object) -> object:
        result = await original(session, **kwargs)  # type: ignore[arg-type]
        await insert_repo_and_grant(
            session,
            tenant_id=env.tenant_id,
            agent_id=env.agent_id,
            repo_id=301,
            installation_id=99,
            owner_id=77,
            full_name="gamma/repo-301",
        )
        return result

    monkeypatch.setattr(github_connect, "activate_confirmed_agent", stage_then_leak)
    failed = await env.post(hidden, remove=(101,), add=(201,))

    assert failed.status_code == 400
    assert await env.grants() == {101: False}
    assert await env.invitation_active(hidden)


@pytest.mark.asyncio
async def test_a_chat_removal_racing_a_switch_leaves_one_installation(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    _state, _page, hidden = await env.editor(org="88")

    async def chat_removal() -> None:
        async with env.sessionmaker.begin() as session:
            await github_access.remove_agent_repo(
                session,
                tenant_id=env.tenant_id,
                agent_id=env.agent_id,
                repo_id=101,
                account_id=None,
            )

    switch, _ = await asyncio.gather(
        env.post(hidden, remove=(101, 102), add=(201,)), chat_removal()
    )

    # 403: the chat write landed before the request read the grants, so 101 is no grant.
    # 409: it landed between that read and the transaction, so the snapshot moved.
    assert switch.status_code in (200, 403, 409)
    assert len(await _installation_ids(env)) <= 1
    # Either the switch won, or the chat write changed the snapshot first and it did not.
    assert set(await env.grants()) == ({201} if switch.status_code == 200 else {102})


# --- The account or org choice, and the switch built on it --------------------------

_CHOOSER_TITLE = "Choose an account or org"
_CHOOSER_LINE = "Editor can use repos from one GitHub account or org at a time."
_NOT_LISTED = "Account or org not listed?"


def _org_links(page_html: str) -> dict[str, str]:
    """Login -> the submit value of each choosable row of the account or org choice."""
    return {
        html.unescape(login): html.unescape(value)
        for value, login in re.findall(
            r'<button class="gh-org-link" type="submit" name="(?:org|navigate)" value="([^"]+)">'
            r'.*?<span class="gh-org-name">([^<]+)</span>',
            page_html,
            flags=re.S,
        )
    }


async def _pick(env: _Env, chooser_html: str, login: str, state: str = "") -> httpx.Response:
    """Choose a row as a browser would: a GET form when opened fresh, else a POST carrying state."""
    value = _org_links(_text_of(chooser_html))[login]
    if 'id="org-choice" method="get"' in chooser_html:
        assert value.isdigit()
        return await env.get(state, org=value)
    return await _submit(env, [*_hidden_pairs(chooser_html), ("navigate", value)])


def _text_of(page_html: str) -> str:
    return html.unescape(page_html)


def _org_rows(page_html: str) -> list[str]:
    return re.findall(r'<li class="gh-org-row[^"]*"', page_html)


def _noscript_submission(
    page_html: str, *, tick: tuple[int, ...] = (), button: tuple[str, str] | None = None
) -> list[tuple[str, str]]:
    """What a browser without scripting submits: the form's own fields, ticked boxes and select.

    `button` is the submit button pressed, if it has a name.
    """
    pairs = _hidden_pairs(page_html)
    match = re.search(r"<noscript>(.*?)</noscript>", page_html, flags=re.S)
    assert match is not None
    fallback = match.group(1)
    for name, value, checked in re.findall(
        r'<input type="checkbox" name="(remove|repo)" value="(\d+)"( checked)?>', fallback
    ):
        if checked or (name == "repo" and int(value) in tick):
            pairs.append((name, value))
    pairs += re.findall(r'<input type="hidden" name="(access_\d+)" value="([^"]*)">', fallback)
    select = re.search(r'<select name="working">(.*?)</select>', fallback, flags=re.S)
    assert select is not None
    options = re.findall(r'<option value="([^"]*)"( selected)?>', select.group(1))
    chosen = next((value for value, selected in options if selected), options[0][0])
    pairs.append(("working", chosen))
    if button is not None:
        pairs.append(button)
    return pairs


async def _submit(env: _Env, pairs: list[tuple[str, str]]) -> httpx.Response:
    return await env.browser.post(
        "/oauth/github/confirm",
        content=urlencode(pairs),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.asyncio
async def test_an_agent_with_no_repos_chooses_an_account_or_org_first(env: _Env) -> None:
    env.orgs[99].kind = "User"
    state = await env.start()
    before = len(env.github_calls)

    page = await env.get(state)

    assert page.status_code == 200
    # One call: the installation list. No repo is listed until one is chosen.
    assert env.github_calls[before:] == ["/user/installations"]
    text_ = _text(page)
    assert f"<h1>{_CHOOSER_TITLE}</h1>" in text_ and _CHOOSER_LINE in text_
    links = _org_links(text_)
    assert links == {"acme": "77", "beta": "88", "gamma": "99"}
    # Opened fresh it is a plain GET form that sends only the state and the pick.
    assert 'id="org-choice" method="get"' in text_
    assert _hidden_pairs(text_) == [("state", state)]
    # Avatar and name, no repo counts; organisations square, people round.
    assert 'src="https://avatars.githubusercontent.com/u/55?v=4"' in text_
    assert text_.count("gh-org-avatar--user") == 1
    assert "repos</" not in text_.split("gh-org-list", 1)[1].split("Account or org not listed?")[0]
    assert "gh-picker-choice" not in text_ and "repo-picker" not in text_
    # The last row sends the person to GitHub's own install page: the link the editor uses.
    assert _NOT_LISTED in text_ and "Open GitHub setup" in text_
    setup = re.search(r'href="([^"]+)">Open GitHub setup', text_)
    assert setup is not None
    assert setup.group(1).startswith("https://github.com/apps/sample-app/installations/new?")
    assert "cancel=1" in text_  # Cancel backs out of the connection
    assert "Search accounts and orgs" not in text_


@pytest.mark.asyncio
async def test_an_avatar_that_is_not_githubs_is_not_loaded(env: _Env) -> None:
    env.orgs[77].avatar = "https://evil.test/a.png"
    state = await env.start()

    page = await env.get(state)

    assert "evil.test" not in page.text
    assert '<span class="gh-org-avatar" aria-hidden="true">A</span>' in page.text


@pytest.mark.asyncio
async def test_a_suspended_installation_is_listed_but_cannot_be_chosen(env: _Env) -> None:
    env.orgs[88].suspended = True
    state = await env.start()

    page = await env.get(state)

    text_ = _text(page)
    assert set(_org_links(text_)) == {"acme", "gamma"}
    paused = re.search(r'<li class="gh-org-row gh-org-row--paused".*?</li>', text_, flags=re.S)
    assert paused is not None
    assert "beta" in paused.group(0) and "Paused on GitHub" in paused.group(0)
    assert 'role="link" aria-disabled="true"' in paused.group(0)
    assert "<a " not in paused.group(0) and "org=88" not in text_
    # Following the link anyway is refused by the server, whatever the page said.
    refused = await env.get(state, org="88")
    assert refused.status_code == 403
    assert env.repo_calls() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [6, 7])
async def test_search_appears_only_with_more_than_six_rows_and_never_hides_the_list(
    env: _Env, rows: int
) -> None:
    for number in range(4, rows + 1):
        env.orgs[100 + number] = _Org(f"org-{number}", 200 + number, {})
    env.orgs[100 + rows].suspended = True  # search covers paused rows too
    assert len(env.orgs) == rows
    state = await env.start()

    page = await env.get(state)

    text_ = _text(page)
    assert len(_org_rows(text_)) == rows + 1  # every installation, then "not listed"
    searchable = rows > 6
    assert ('id="search-orgs"' in text_) is searchable
    if searchable:
        # A labelled field that only a scripting browser shows; the list is already complete.
        assert 'aria-label="Search accounts and orgs"' in text_
        assert 'placeholder="Search accounts and orgs"' in text_
        assert "gh-org-search gh-js" in text_
        assert "No accounts or orgs match '" in text_  # built by the script from the query
        assert 'data-org="org-7"' in text_
    else:
        assert "<script>" not in text_


@pytest.mark.asyncio
async def test_choosing_a_row_opens_the_editor_for_that_verified_installation(env: _Env) -> None:
    state = await env.start()
    links = _org_links(_text(await env.get(state)))
    before = len(env.github_calls)

    page = await env.get(state, org=links["beta"])

    assert page.status_code == 200
    assert env.github_calls[before:] == [
        "/user/installations",
        "/user/installations/88/repositories",
    ]
    assert ("installation", "88") in _hidden_pairs(page.text)
    assert "beta/repo-201" in page.text and "acme/repo-101" not in page.text
    assert "Add beta repos" in page.text
    # Choosing again goes back to the choice, not past it, carrying the form with it.
    assert (
        '<button class="gh-link-button" type="submit" name="navigate" value="choose" '
        "formnovalidate>Choose another account or org</button>"
    ) in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("org", ["12345", "abc", "0", "77x"])
async def test_a_forged_org_is_refused_whatever_the_choice_showed(env: _Env, org: str) -> None:
    state = await env.start()

    page = await env.get(state, org=org)

    assert page.status_code in (400, 403)
    assert env.repo_calls() == []


@pytest.mark.asyncio
async def test_one_usable_installation_skips_the_choice(env: _Env) -> None:
    # The choice would have a single usable row. The page shows that installation's repos,
    # as it would after a click, and `choose=1` still reaches the choice.
    del env.orgs[88], env.orgs[99]
    state = await env.start()

    page = await env.get(state)

    assert "gh-org-list" not in page.text
    assert ("installation", "77") in _hidden_pairs(page.text)
    assert "acme/repo-101" in page.text
    assert "Choose another account or org" not in page.text  # nothing else to choose

    forced = await env.get(state, choose="1")
    assert set(_org_links(_text(forced))) == {"acme"}


@pytest.mark.asyncio
async def test_a_paused_installation_alongside_one_usable_one_still_skips_the_choice(
    env: _Env,
) -> None:
    del env.orgs[99]
    env.orgs[88].suspended = True
    state = await env.start()

    page = await env.get(state)

    assert ("installation", "77") in _hidden_pairs(page.text)


@pytest.mark.asyncio
async def test_only_paused_installations_still_show_the_choice(env: _Env) -> None:
    for org in env.orgs.values():
        org.suspended = True
    state = await env.start()

    page = await env.get(state)

    text_ = _text(page)
    assert _org_links(text_) == {}
    assert text_.count("Paused on GitHub") == 3 and _NOT_LISTED in text_


@pytest.mark.asyncio
async def test_no_installations_at_all_still_gets_the_editor_and_setup_link(env: _Env) -> None:
    env.orgs.clear()
    state = await env.start()

    page = await env.get(state)

    assert page.status_code == 200 and "gh-org-list" not in page.text
    assert "Missing a repo? Add it to the Daimon GitHub App on GitHub." in _text(page)


@pytest.mark.asyncio
async def test_a_requested_repo_names_its_org_so_the_choice_is_skipped(env: _Env) -> None:
    state = await env.start(requested_repo="beta/repo-202")

    page = await env.get(state)

    assert "gh-org-list" not in page.text
    assert ("installation", "88") in _hidden_pairs(page.text)
    # The requested repo is ticked for the person to confirm.
    assert re.search(r'value="202" checked>', page.text)
    assert "Choose another account or org" in page.text  # other orgs remain reachable


@pytest.mark.asyncio
async def test_a_requested_repo_in_an_org_not_in_the_list_gets_the_choice(env: _Env) -> None:
    state = await env.start(requested_repo="elsewhere/thing")

    page = await env.get(state)

    assert set(_org_links(_text(page))) == {"acme", "beta", "gamma"}


# The switch: a pinned agent moves to another account or org in one save.


@pytest.mark.asyncio
async def test_a_pinned_editor_offers_the_switch_in_a_collapsed_disclosure(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    _state, page, _hidden = await env.editor()

    text_ = _text(page)
    assert (
        '<details id="switch-org" class="gh-switch"><summary>Use another account or org?' in text_
    )
    assert "Editor has acme repos." in text_
    assert "Remove all and switch" in text_ and "Nothing changes until you save." in text_
    # A submit button of the editor's own form, so pending changes travel with it.
    assert (
        '<button class="gh-add-button" type="submit" name="navigate" value="choose" '
        "formnovalidate>Remove all and switch</button>"
    ) in text_
    assert "Switch to" not in text_.split("<script>")[0]  # no destination chosen yet
    # Nothing is marked until a destination is chosen.
    assert 'data-switching="true"' not in text_ and " checked>" not in text_.split("<noscript>")[1]


@pytest.mark.asyncio
async def test_the_switch_choice_lists_the_other_installations_without_repo_calls(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    env.orgs[99].suspended = True
    state = await env.start()
    before = len(env.github_calls)

    page = await env.get(state, choose="1")

    text_ = _text(page)
    assert env.github_calls[before:] == ["/user/installations"]
    assert set(_org_links(text_)) == {"beta"}  # not the pinned org; gamma is paused
    assert "Paused on GitHub" in text_ and _NOT_LISTED in text_
    # Cancel here goes back to the saved repos, not out of the connection.
    cancel = re.search(r'<a class="gh-link" href="([^"]+)">Cancel', text_)
    assert cancel is not None and "cancel=1" not in cancel.group(1)
    assert parse_qs(urlparse(cancel.group(1)).query) == {"state": [state]}


@pytest.mark.asyncio
async def test_the_switch_page_marks_every_saved_repo_and_a_plain_form_saves_the_switch(
    env: _Env,
) -> None:
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    await env.grant(77, 103, status="revoked")
    state = await env.start()
    chooser = await env.get(state, choose="1")

    page = await _pick(env, chooser.text, "beta", state)

    assert page.status_code == 200
    # Every saved repo, revoked ones included, is ticked to remove with no script involved.
    fallback = re.search(r"<noscript>(.*?)</noscript>", page.text, flags=re.S)
    assert fallback is not None
    ticked = re.findall(r'name="remove" value="(\d+)" checked>', fallback.group(1))
    assert sorted(ticked) == ["101", "102", "103"]
    assert 'data-switching="true"' in page.text and 'data-switch-owner="beta"' in page.text
    assert "Saving removes every acme repo and adds the beta repos you tick." in page.text
    assert "beta/repo-201" in fallback.group(1) and "acme/repo-101" in page.text
    # The working-repo select stays on its default; the server then makes the first repo
    # in the form, in page order, the working repo.
    submission = _noscript_submission(page.text, tick=(202, 201))
    assert [value for name, value in submission if name == "working"] == ["keep"]

    saved = await _submit(env, submission)

    assert saved.status_code == 200, saved.text
    assert await _installation_ids(env) == {88}
    assert await env.grants() == {201: True, 202: False}


@pytest.mark.asyncio
async def test_a_plain_form_that_unticks_a_saved_repo_cannot_switch(env: _Env) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102)
    state = await env.start()
    page = await env.get(state, org="88")
    submission = _noscript_submission(page.text, tick=(201,))

    partial = [pair for pair in submission if pair != ("remove", "102")]
    refused = await _submit(env, partial)

    assert refused.status_code == 403
    assert await env.grants() == {101: False, 102: False}
    assert await _installation_ids(env) == {77}
    assert await env.invitation_active(_hidden_pairs(page.text))

    # No removal at all, or a removal of someone else's repo, is no better.
    for tampered in (
        [pair for pair in submission if pair[0] != "remove"],
        [*submission, ("remove", "201")],
    ):
        assert (await _submit(env, tampered)).status_code == 403
    assert await _installation_ids(env) == {77}


@pytest.mark.asyncio
async def test_a_switch_page_with_nothing_to_remove_marks_nothing(env: _Env) -> None:
    # A no-pin agent choosing an installation is not switching: nothing to pre-mark.
    state = await env.start()

    page = await env.get(state, org="88")

    assert 'data-switching="true"' not in page.text
    assert " checked>" not in page.text.split("<noscript>")[1]


@pytest.mark.asyncio
async def test_a_stale_pinned_agent_can_still_switch_from_its_disclosure(env: _Env) -> None:
    await env.grant(77, 101)
    env.orgs[77].suspended = True
    _state, page, _hidden = await env.editor()

    text_ = _text(page)
    assert _STALE_TITLE in text_ and "Use another account or org?" in text_
    assert "Editor has acme repos." in text_
    moved = await env.get(_state, org="88")
    assert 'name="remove" value="101" checked>' in moved.text
    saved = await _submit(env, _noscript_submission(moved.text, tick=(201,)))
    assert saved.status_code == 200, saved.text
    assert await _installation_ids(env) == {88}


@pytest.mark.asyncio
async def test_a_requested_repo_in_another_org_says_so_and_opens_the_switch(env: _Env) -> None:
    await env.grant(77, 101)
    _state, page, _hidden = await env.editor(requested_repo="beta/repo-201")

    text_ = _text(page)
    assert "beta/repo-201 is in beta, but Editor has acme repos." in text_
    assert "To add it, switch orgs below." in text_
    assert '<details id="switch-org" class="gh-switch" open>' in text_
    # Nothing from the other org is offered here; adds still come from the pinned one.
    assert 'data-name="beta/repo-201"' not in text_


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["acme/repo-102", "ACME/repo-102", None, "no-slash", "a/b/c"])
async def test_a_requested_repo_in_the_pinned_org_or_malformed_leaves_the_switch_closed(
    env: _Env, requested: str | None
) -> None:
    await env.grant(77, 101)
    _state, page, _hidden = await env.editor(requested_repo=requested)

    text_ = _text(page)
    assert ", but Editor has acme repos." not in text_
    assert '<details id="switch-org" class="gh-switch">' in text_


@pytest.mark.asyncio
async def test_the_requested_repo_is_ticked_once_the_org_is_chosen(env: _Env) -> None:
    await env.grant(77, 101)
    state = await env.start(requested_repo="beta/repo-201")

    page = await env.get(state, org="88")

    assert re.search(r'value="201" checked>', page.text)
    assert "is in beta, but" not in page.text  # no longer a request for another org


@pytest.mark.asyncio
async def test_a_failed_switch_retry_keeps_the_destination_and_every_removal(env: _Env) -> None:
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    state = await env.start()
    page = await env.get(state, org="88")
    submission = _noscript_submission(page.text, tick=(201, 202))
    env.details_down = True

    failed = await _submit(env, submission)

    assert failed.status_code == 502
    retry = _hidden_pairs(failed.text)
    assert ("installation", "88") in retry
    assert sorted(value for name, value in retry if name == "remove") == ["101", "102"]
    assert sorted(value for name, value in retry if name == "repo") == ["201", "202"]
    assert dict(retry)["snapshot"] == dict(submission)["snapshot"]
    assert await env.grants() == {101: True, 102: False}

    env.details_down = False
    again = await _submit(env, retry)
    assert again.status_code == 200, again.text
    assert await env.grants() == {201: True, 202: False}
    assert await _installation_ids(env) == {88}


@pytest.mark.asyncio
async def test_a_retry_after_the_agents_repos_changed_is_not_rebased_onto_the_new_snapshot(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    state = await env.start()
    page = await env.get(state, org="88")
    submission = _noscript_submission(page.text, tick=(201,))
    env.details_down = True
    failed = await _submit(env, submission)
    assert failed.status_code == 502
    retry = _hidden_pairs(failed.text)

    # A chat command adds another source repo while the person waits.
    await env.grant(77, 103)
    env.details_down = False
    again = await _submit(env, retry)

    # The switch still removes only what the person saw, so it is refused, not widened.
    assert again.status_code in (403, 409)
    assert await env.grants() == {101: False, 103: False}
    assert await _installation_ids(env) == {77}


@pytest.mark.asyncio
async def test_the_switch_script_marks_every_row_and_discard_goes_back_to_the_saved_page(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    state = await env.start()
    page = await env.get(state, org="88")

    reset = re.search(r'data-reset-url="([^"]+)"', page.text)
    assert reset is not None
    assert parse_qs(urlparse(html.unescape(reset.group(1))).query) == {"state": [state]}
    script = page.text.split("<script>", 1)[1]
    # The scripting path shares the page's behaviour: every row marked, Discard back to the
    # saved page, the destination chip only once something is added, no per-row undo.
    assert 'form.dataset.removed || "").split(",")' in script
    assert "location.assign(resetUrl)" in script
    assert (
        'if (added && switchOwner) items.push(chip("Switch to " + switchOwner + " on save"'
        in script
    )
    assert '" to remove"' in script and '" to add"' in script and '"New working repo: "' in script
    assert 'id="pending-chips"' in page.text
    # The saved page that Discard returns to is the pinned editor, not a switch.
    saved = await env.browser.get(html.unescape(reset.group(1)))
    assert 'data-switching="true"' not in saved.text
    assert 'id="pending-chips"' not in saved.text
    assert "Remove all and switch" in saved.text


# Pending changes survive the trip to the account or org choice and back.


async def _staged_switch(env: _Env) -> tuple[str, list[tuple[str, str]], httpx.Response]:
    """Open the beta switch page and post it the way scripting does: marks, adds, working repo."""
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    state = await env.start()
    page = await env.get(state, org="88")
    assert page.status_code == 200
    pairs = [
        *_hidden_pairs(page.text),
        ("remove", "101"),
        ("remove", "102"),
        ("repo", "202"),
        ("access_202", "write"),
        ("working", "202"),
    ]
    return state, pairs, page


@pytest.mark.asyncio
async def test_choosing_another_account_and_picking_the_same_one_keeps_the_staged_switch(
    env: _Env,
) -> None:
    _state, pairs, page = await _staged_switch(env)

    chooser = await _submit(env, [*pairs, ("navigate", "choose")])

    assert chooser.status_code == 200 and "gh-org-list" in chooser.text
    carried = _hidden_pairs(chooser.text)
    # The editor's own fields, snapshot and signature included, ride in the choice form
    # once; the rows only add which installation was picked.
    for pair in (("remove", "101"), ("remove", "102"), ("repo", "202"), ("working", "202")):
        assert pair in carried
    assert dict(carried)["snapshot"] == dict(pairs)["snapshot"]
    assert dict(carried)["snapshot_sig"] == dict(pairs)["snapshot_sig"]
    assert not [name for name, _ in carried if name == "navigate"]
    assert 'id="org-choice" method="post"' in chooser.text
    # Every installation but the pinned one is offered.
    assert _org_links(_text(chooser)) == {"beta": "to:88", "gamma": "to:99"}
    # Going to the choice saved nothing and spent nothing.
    assert await env.grants() == {101: True, 102: False}
    assert await env.invitation_active(_hidden_pairs(page.text))

    back = await _pick(env, chooser.text, "beta")

    text_ = _text(back)
    assert back.status_code == 200 and 'data-switching="true"' in text_
    assert 'data-removed="101,102"' in text_
    assert 'data-staged="202"' in text_ and 'data-carried-working="202"' in text_
    submission = _noscript_submission(back.text)
    assert [(n, v) for n, v in submission if n == "repo"] == [("repo", "202")]
    assert ("working", "202") in submission
    assert sorted(v for n, v in submission if n == "remove") == ["101", "102"]
    assert ("installation", "88") in submission

    saved = await _submit(env, submission)
    assert saved.status_code == 200, saved.text
    assert await env.grants() == {202: True}
    assert await _installation_ids(env) == {88}


@pytest.mark.asyncio
async def test_picking_a_different_account_drops_adds_the_new_one_does_not_list(env: _Env) -> None:
    _state, pairs, _page = await _staged_switch(env)
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])

    gamma = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", "to:99")])

    text_ = _text(gamma)
    assert gamma.status_code == 200 and "Add gamma repos" in text_
    # Beta's repo is not a gamma repo: it does not come along, nor does the working choice.
    assert "data-staged" not in text_ and "data-carried-working" not in text_
    assert 'data-removed="101,102"' in text_  # every saved repo still opens marked
    submission = _noscript_submission(gamma.text)
    assert not [n for n, _ in submission if n == "repo"]
    assert ("working", "keep") in submission


@pytest.mark.asyncio
async def test_a_grant_added_while_choosing_is_refused_not_folded_into_the_removals(
    env: _Env,
) -> None:
    _state, pairs, page = await _staged_switch(env)
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])
    assert chooser.status_code == 200

    await env.grant(77, 103)  # a chat command adds a source repo during the navigation
    stale_destination = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", "to:88")])
    stale_return = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", "return")])
    stale_choice = await _submit(env, [*pairs, ("navigate", "choose")])

    for refused in (stale_destination, stale_return, stale_choice):
        assert refused.status_code == 409
        assert "The agent's repos changed. Reload and try again." in _text(refused)
        assert "data-removed" not in refused.text
    assert await env.grants() == {101: True, 102: False, 103: False}
    assert await _installation_ids(env) == {77}
    assert await env.invitation_active(_hidden_pairs(page.text))


@pytest.mark.asyncio
async def test_carried_state_needs_the_signed_snapshot(env: _Env) -> None:
    _state, pairs, _page = await _staged_switch(env)

    for tampered in (
        [pair for pair in pairs if pair[0] != "snapshot_sig"],
        [pair for pair in pairs if pair[0] != "snapshot"],
        [("snapshot_sig", "0" * 64) if pair[0] == "snapshot_sig" else pair for pair in pairs],
        [("snapshot", "1" * 64) if pair[0] == "snapshot" else pair for pair in pairs],
    ):
        refused = await _submit(env, [*tampered, ("navigate", "choose")])
        assert refused.status_code == 400
    assert await env.grants() == {101: True, 102: False}


@pytest.mark.asyncio
@pytest.mark.parametrize("navigate", ["to:", "to:abc", "to:-1", "to:٣", "go", "to:77:88", "CHOOSE"])
async def test_a_malformed_navigation_is_refused(env: _Env, navigate: str) -> None:
    _state, pairs, _page = await _staged_switch(env)

    refused = await _submit(env, [*pairs, ("navigate", navigate)])

    assert refused.status_code == 400
    assert await env.grants() == {101: True, 102: False}


@pytest.mark.asyncio
async def test_navigating_to_an_installation_that_is_not_in_the_list_is_refused(env: _Env) -> None:
    _state, pairs, _page = await _staged_switch(env)
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])
    env.orgs[99].suspended = True

    for target in ("to:12345", "to:99", "to:77x"):
        refused = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", target)])
        assert refused.status_code in (400, 403), target
    assert await env.grants() == {101: True, 102: False}


@pytest.mark.asyncio
async def test_navigation_is_not_a_save_and_a_failed_one_retries_as_navigation(env: _Env) -> None:
    _state, pairs, page = await _staged_switch(env)
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])
    env.listing_down = True

    failed = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", "to:88")])

    assert failed.status_code == 502
    retry = _hidden_pairs(failed.text)
    assert ("navigate", "to:88") in retry and ("repo", "202") in retry
    env.listing_down = False
    again = await _submit(env, retry)
    # Retrying opens the page again. It does not save what was staged.
    assert again.status_code == 200 and 'data-switching="true"' in again.text
    assert await env.grants() == {101: True, 102: False}
    assert await env.invitation_active(_hidden_pairs(page.text))


@pytest.mark.asyncio
async def test_cancelling_the_choice_returns_to_the_editor_with_its_pending_removals(
    env: _Env,
) -> None:
    await env.grant(77, 101)
    await env.grant(77, 102, working=True)
    await env.grant(77, 103)
    state, page, hidden = await env.editor()
    pairs = [*hidden, ("remove", "101"), ("working", "103")]

    chooser = await _submit(env, [*pairs, ("navigate", "choose")])
    assert chooser.status_code == 200
    assert '<button class="gh-link" type="submit" name="navigate" value="return">' in chooser.text
    back = await _submit(env, [*_hidden_pairs(chooser.text), ("navigate", "return")])

    text_ = _text(back)
    assert back.status_code == 200 and 'data-switching="true"' not in text_
    assert 'data-removed="101"' in text_ and 'data-carried-working="103"' in text_
    submission = _noscript_submission(back.text)
    assert [v for n, v in submission if n == "remove"] == ["101"]
    assert ("working", "103") in submission
    assert await env.grants() == {101: False, 102: True, 103: False}
    del state, page


@pytest.mark.asyncio
async def test_a_navigation_post_on_a_connection_without_an_agent_is_refused(env: _Env) -> None:
    state = await env.start(agent=False)
    page = await env.get(state)
    assert page.status_code == 200

    refused = await _submit(env, [*_hidden_pairs(page.text), ("navigate", "choose")])

    assert refused.status_code == 400


# A page without scripting says what it will do.


@pytest.mark.asyncio
async def test_the_switch_page_without_scripting_states_its_pending_changes(env: _Env) -> None:
    await env.grant(77, 101, working=True)
    await env.grant(77, 102)
    state = await env.start()

    page = await env.get(state, org="88")

    text_ = _text(page)
    status = re.search(r'<strong id="selection-status"[^>]*>([^<]*)</strong>', text_)
    assert status is not None and status.group(1) == "Not saved yet"
    assert "No changes to save" not in text_.split("<script>")[0]
    assert re.findall(r'<li class="gh-chip[^"]*">([^<]*)</li>', text_) == ["2 to remove"]
    assert (
        'id="pending-chips" class="gh-chips" aria-label="Pending changes" aria-live="polite">'
        in (text_)
    )
    # "Keep current" would be false: with adds the server makes the first one the working repo.
    select = re.search(r'<select name="working">(.*?)</select>', text_, flags=re.S)
    assert select is not None
    assert '<option value="keep" selected>' not in select.group(1)
    assert '<option value="keep">First added repo</option>' in select.group(1)
    assert "Keep current" not in select.group(1)
    # A plain link goes back to the saved editor; Cancel still ends the flow.
    discard = re.search(
        r'<a id="discard-link" class="gh-editor-discard gh-nojs" href="([^"]+)"', text_
    )
    assert discard is not None
    assert parse_qs(urlparse(discard.group(1)).query) == {"state": [state]}
    assert "cancel=1" in text_
    saved = await env.browser.get(discard.group(1))
    assert saved.status_code == 200 and 'data-switching="true"' not in saved.text
    assert "No changes to save" in saved.text and "discard-link" not in saved.text
    assert '<option value="keep">Keep current</option>' in saved.text


@pytest.mark.asyncio
async def test_a_carried_page_without_scripting_counts_the_adds_too(env: _Env) -> None:
    _state, pairs, _page = await _staged_switch(env)
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])

    back = await _pick(env, chooser.text, "beta")

    text_ = _text(back)
    assert re.findall(r'<li class="gh-chip[^"]*">([^<]*)</li>', text_) == [
        "1 to add",
        "2 to remove",
        "Switch to beta on save",
        "New working repo: beta/repo-202",
    ]
    assert re.search(r'<option value="202" selected>', text_)
    assert re.search(r'name="repo" value="202" checked>', text_)
    assert not re.search(r'name="repo" value="201" checked>', text_)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "navigate",
    [
        [("navigate", "")],
        [("navigate", ""), ("navigate", "choose")],
        [("navigate", "choose"), ("navigate", "")],
        [("navigate", "choose"), ("navigate", "choose")],
        [("navigate", "return"), ("navigate", "choose")],
    ],
)
async def test_an_empty_or_repeated_navigate_field_never_saves(
    env: _Env, navigate: list[tuple[str, str]]
) -> None:
    await env.grant(77, 101, working=True)
    state = await env.start()
    page = await env.get(state, org="88")
    valid_save = _noscript_submission(page.text, tick=(201,))
    # Without the field this payload is a complete, valid save.
    assert [pair for pair in valid_save if pair[0] == "navigate"] == []

    refused = await _submit(env, [*valid_save, *navigate])

    assert refused.status_code == 400
    assert await env.grants() == {101: True}
    assert await _installation_ids(env) == {77}
    assert await env.invitation_active(_hidden_pairs(page.text))

    saved = await _submit(env, valid_save)
    assert saved.status_code == 200 and await env.grants() == {201: True}


@pytest.mark.asyncio
async def test_the_working_repo_a_move_defaults_to_survives_the_choice_in_addition_order(
    env: _Env,
) -> None:
    await env.grant(77, 101, working=True)
    state = await env.start()
    page = await env.get(state, org="88")
    # Added 202 first, then 201: the form lists them in that order and "keep" means 202.
    pairs = [
        *_hidden_pairs(page.text),
        ("remove", "101"),
        ("repo", "202"),
        ("access_202", "write"),
        ("repo", "201"),
        ("access_201", "write"),
        ("working", "keep"),
    ]
    chooser = await _submit(env, [*pairs, ("navigate", "choose")])
    assert chooser.status_code == 200

    back = await _pick(env, chooser.text, "beta")

    text_ = _text(back)
    # The order is carried and the default is carried as the repo itself, not re-derived
    # from the alphabetical list the page offers.
    assert 'data-staged="202,201"' in text_ and 'data-carried-working="202"' in text_
    assert re.findall(r'<li class="gh-chip[^"]*">([^<]*)</li>', text_)[-1] == (
        "New working repo: beta/repo-202"
    )
    assert re.search(r'<option value="202" selected>', text_)
    submission = _noscript_submission(back.text)
    assert ("working", "202") in submission
    saved = await _submit(env, submission)
    assert saved.status_code == 200, saved.text
    assert await env.grants() == {201: False, 202: True}


@pytest.mark.asyncio
async def test_a_move_saved_without_the_trip_makes_the_first_added_repo_the_working_repo(
    env: _Env,
) -> None:
    await env.grant(77, 101, working=True)
    state = await env.start()
    page = await env.get(state, org="88")
    direct = await _submit(
        env,
        [
            *_hidden_pairs(page.text),
            ("remove", "101"),
            ("repo", "202"),
            ("access_202", "write"),
            ("repo", "201"),
            ("access_201", "write"),
            ("working", "keep"),
        ],
    )
    assert direct.status_code == 200, direct.text
    assert (await env.grants())[202] is True
