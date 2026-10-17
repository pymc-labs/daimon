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

    async def start(self, *, agent: bool = True) -> str:
        """Mint a fresh invitation, sign in, and return the flow's state."""
        if not agent:
            return await self._sign_in(await self._mint(agent=False))
        return await self._sign_in(await self._mint(agent=True))

    async def _mint(self, *, agent: bool) -> str:
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
            )

    async def _sign_in(self, invitation: str) -> str:
        start = await self.browser.get(f"/oauth/github/connect/{invitation}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        await self.browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        return state

    async def get(self, state: str, **params: str) -> httpx.Response:
        return await self.browser.get("/oauth/github/confirm", params={"state": state, **params})

    async def editor(self, **params: str) -> tuple[str, httpx.Response, list[tuple[str, str]]]:
        """Open the editor; return the state, the page and its hidden fields."""
        state = await self.start()
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
                    "account": {"id": org.owner_id, "login": org.login, "type": "Organization"},
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
async def test_agent_without_grants_still_lists_every_installation(env: _Env) -> None:
    _state, page, hidden = await env.editor()

    assert env.github_calls == [
        "/user/installations",
        "/user/installations/77/repositories",
        "/user/installations/88/repositories",
        "/user/installations/99/repositories",
    ]
    assert "beta/repo-201" in page.text
    assert not [name for name, _ in hidden if name == "installation"]


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
