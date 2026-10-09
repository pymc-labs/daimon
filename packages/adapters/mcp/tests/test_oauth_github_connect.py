"""Browser GitHub connection routes with a mocked GitHub API."""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp import oauth_github
from daimon.adapters.mcp.oauth_github import build_oauth_github_routes
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_connect_delivery import claim_next, settle
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import set_external, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


def _settings(key: str, *, configured: bool = True) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=(
            GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            )
            if configured
            else GithubAppSettings()
        ),
    )


def test_routes_not_mounted_when_unconfigured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    app = create_mcp_app(
        settings=_settings(key, configured=False),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    paths = [route.path for route in app.routes if isinstance(route, Route)]
    assert "/oauth/github/connect/{token}" not in paths
    assert "/oauth/github/confirm" not in paths


def test_picker_names_owner_type_for_screen_readers() -> None:
    installations = [
        oauth_github._Installation(
            id=index,
            owner_id=index,
            owner_login=login,
            repository_selection="selected",
            repos=(oauth_github._Repo(index, index, index, f"{login}/repo", True),),
            owner_type=owner_type,
        )
        for index, login, owner_type in (
            (1, "company", "Organization"),
            (2, "person", "User"),
        )
    ]
    page = oauth_github._confirmation_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        cancel_url="https://discord.com",
        installations=installations,
        clients_present=False,
        platform="discord",
        workspace="Test Server",
        agent_name="Test Agent",
    )
    assert '<span class="web-sr-only">Organization</span>company' in page.body.decode()
    assert '<span class="web-sr-only">Personal account</span>person' in page.body.decode()
    assert page.body.decode().count('class="web-icon web-icon--github"') >= 4
    assert '<div class="gh-context"><svg class="web-icon web-icon--discord"' in page.body.decode()
    assert (
        '<button class="gh-primary" id="connect-repos" type="submit"><svg class="web-icon web-icon--github"'
        in page.body.decode()
    )


def test_done_page_names_github_and_discord_with_marks() -> None:
    page = oauth_github._done_page(
        count=2,
        platform="discord",
        external_id="123",
        requester_label="Alex",
        same_person=True,
        agent_name="ResearchBot",
        update_pending=False,
    )
    body = page.body.decode()
    assert '<h1 class="gh-title-marked"><svg class="web-icon web-icon--github"' in body
    assert 'class="web-icon web-icon--discord"' in body
    assert "Back to Discord" in body


@pytest.mark.asyncio
async def test_pending_installation_request_matches_signed_in_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def app_jwt(private_key: str, app_id: str, *, now: int) -> str:
        return "app-jwt"

    monkeypatch.setattr(oauth_github, "build_app_jwt", app_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installation-requests"
        assert request.headers["authorization"] == "Bearer app-jwt"
        return httpx.Response(
            200,
            json=[
                {"requester": {"id": 99}, "account": {"login": "another-org"}},
                {"requester": {"id": 17}, "account": {"login": "example"}},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await oauth_github.has_pending_installation_request(
            client, app_id="42", private_key="pem", github_user_id=17
        ) == oauth_github._PendingInstallationRequest(True, "example")
        assert await oauth_github.has_pending_installation_request(
            client, app_id="42", private_key="pem", github_user_id=20
        ) == oauth_github._PendingInstallationRequest(False)


def test_pending_page_names_verified_organization_safely() -> None:
    page = oauth_github._pending_page("#check", "#cancel", "team<one>")
    text = page.body.decode()
    assert "An owner of team&lt;one&gt; must approve Daimon." in text
    assert "team<one>" not in text


@pytest.mark.parametrize("revoke_failure_status", [None, 503, 404])
@pytest.mark.asyncio
async def test_cancel_after_oauth_revokes_user_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    revoke_failure_status: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancel after GitHub issues a token must remove that token's App grant."""
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="cancel-workspace")
        await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        invitation_token = await github_connect.mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
            requester_label="Alex",
            requester_platform_user_id="123",
            origin_platform="discord",
        )
    revoked: list[str] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "cancelled-user-token"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17})
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": []})
        if request.url.path == "/applications/client/token":
            revoked.append(request.content.decode())
            if revoke_failure_status is not None and len(revoked) == 1:
                return httpx.Response(revoke_failure_status)
            return httpx.Response(204)
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    key = Fernet.generate_key().decode()
    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=_settings(key),
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(github_handler)),
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
        start = await browser.get(f"/oauth/github/connect/{invitation_token}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback_response = await browser.get(
            "/oauth/github/callback", params={"state": state, "code": "code"}
        )
        assert callback_response.status_code == 307
        cancelled = await browser.get(
            "/oauth/github/confirm", params={"state": state, "cancel": "1"}
        )
        assert cancelled.status_code == 200
        assert "Nothing was connected." in cancelled.text
        assert ("GitHub connection token revocation failed" in caplog.text) == (
            revoke_failure_status is not None
        )
        async with committing_sessionmaker.begin() as session:
            flow = await github_connect.cancel_flow(
                session, state=state, cookie=browser.cookies["daimon_gh_connect"]
            )
            assert flow is not None
            assert flow.cancelled_at is not None
            assert (flow.encrypted_user_token is not None) == (revoke_failure_status is not None)
            assert not await github_connect.confirm(
                session,
                state=state,
                cookie=browser.cookies["daimon_gh_connect"],
                github_user_id=17,
                repos=[],
            )
            if revoke_failure_status is not None:
                await session.execute(
                    text(
                        "UPDATE github_connect_flows SET expires_at = :expired "
                        "WHERE state_hash = :state_hash"
                    ),
                    {
                        "expired": datetime.now(UTC) - timedelta(seconds=1),
                        "state_hash": github_connect.digest(state),
                    },
                )
                assert (
                    await github_connect.delete_expired_flows(session, now=datetime.now(UTC)) == 0
                )
                await github_connect.create_flow(
                    session,
                    invitation_hash=flow.invitation_hash,
                    state="sibling-flow",
                    cookie="sibling-cookie",
                    encrypted_verifier=b"verifier",
                )
                assert await github_connect.set_user_token(
                    session,
                    state="sibling-flow",
                    encrypted_token=b"sibling-token",
                    github_user_id=17,
                )
                assert await github_connect.confirm(
                    session,
                    state="sibling-flow",
                    cookie="sibling-cookie",
                    github_user_id=17,
                    repos=[],
                )
                pending = await github_connect.cancel_flow(
                    session, state=state, cookie=browser.cookies["daimon_gh_connect"]
                )
                assert pending is not None and pending.encrypted_user_token is not None
        repeated = await browser.get(
            "/oauth/github/confirm", params={"state": state, "cancel": "1"}
        )
        assert repeated.status_code == 200
        assert "Nothing was connected." in repeated.text
        if revoke_failure_status is not None:
            async with committing_sessionmaker() as session:
                flow = await github_connect.cancel_flow(
                    session, state=state, cookie=browser.cookies["daimon_gh_connect"]
                )
                assert flow is not None
                assert flow.encrypted_user_token is None

    assert revoked == ['{"access_token":"cancelled-user-token"}'] * (
        2 if revoke_failure_status is not None else 1
    )


@pytest.mark.asyncio
async def test_connection_happy_path_and_rechecks(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="workspace")
        await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        invitation_token = await github_connect.mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
            requester_label="Alex",
            requester_platform_user_id="123",
            origin_platform="discord",
        )
    repo_admin = True
    repo_two_admin = False
    installation_available = True
    repository_selection = "all"
    installations_available = True
    github_unavailable = False
    approval_pending = False
    requests: list[httpx.Request] = []

    async def pending_check(
        client: httpx.AsyncClient, *, app_id: str, private_key: str, github_user_id: int
    ) -> oauth_github._PendingInstallationRequest:
        assert app_id == "42" and private_key == "pem" and github_user_id == 17
        return oauth_github._PendingInstallationRequest(approval_pending)

    monkeypatch.setattr(oauth_github, "has_pending_installation_request", pending_check)

    def github_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "user-token"})
        if request.url.path == "/applications/client/token":
            assert request.method == "DELETE"
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b'{"access_token":"user-token"}'
            return httpx.Response(204)
        if request.url.path == "/app/installations/77":
            assert request.headers["authorization"] == "Bearer app-jwt"
            if not installation_available:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "account": {"id": 55, "login": "example", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                },
            )
        assert request.headers["authorization"] == "Bearer user-token"
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17})
        if request.url.path == "/user/installations":
            if github_unavailable:
                raise httpx.ConnectError("unavailable")
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 77,
                            "account": {"id": 55, "login": "example", "type": "Organization"},
                            "repository_selection": repository_selection,
                        }
                    ]
                    if installations_available
                    else []
                },
            )
        if request.url.path == "/user/installations/77/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 101,
                            "owner": {"id": 55},
                            "full_name": "example/one",
                            "permissions": {"admin": repo_admin},
                        },
                        {
                            "id": 102,
                            "owner": {"id": 55},
                            "full_name": "example/two",
                            "permissions": {"pull": True, "admin": repo_two_admin},
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    key = Fernet.generate_key().decode()
    settings = _settings(key)
    fernet = build_multifernet((key,))
    monkeypatch.setattr(
        "daimon.adapters.mcp.oauth_github.build_app_jwt",
        lambda *_args, **_kwargs: "app-jwt",
    )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=settings,
        sessionmaker=sessionmaker,
        fernet=fernet,
        client_factory=client_factory,
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
        start = await browser.get(f"/oauth/github/connect/{invitation_token}")
        assert start.status_code == 307
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        assert parse_qs(urlparse(start.headers["location"]).query)["code_challenge_method"] == [
            "S256"
        ]
        assert (
            await browser.get("/oauth/github/callback", params={"state": "bad", "code": "code"})
        ).status_code == 400
        refused_oauth = await browser.get("/oauth/github/callback", params={"state": state})
        assert "Nothing was connected." in refused_oauth.text
        assert "You can close this tab." in refused_oauth.text
        assert "Back to Discord" in refused_oauth.text
        wrong_browser = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        )
        async with wrong_browser:
            assert (
                await wrong_browser.get(
                    "/oauth/github/callback", params={"state": state, "code": "code"}
                )
            ).status_code == 400
        callback_response = await browser.get(
            "/oauth/github/callback", params={"state": state, "code": "code"}
        )
        assert callback_response.status_code == 307
        page = await browser.get("/oauth/github/confirm", params={"state": state})
        assert page.status_code == 200
        assert "Choose repos" in page.text
        assert "0 selected" in page.text
        assert 'id="github-connect-form"' in page.text
        assert "Connecting…" in page.text
        assert "submit.disabled = true" in page.text
        assert "if (connecting || !boxes.some(box => box.checked))" in page.text
        assert 'name="access" value="write" checked' in page.text
        assert 'class="web-icon web-icon--search"' in page.text
        assert 'class="web-icon web-icon--pencil"' in page.text
        assert 'class="web-icon web-icon--github"' in page.text
        assert page.text.index('id="search-repos"') < page.text.index('class="gh-repo-list"')
        assert page.text.count('class="gh-primary"') == 1
        assert 'action="https://mcp.test/oauth/github/confirm"' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["referrer-policy"] == "no-referrer"
        malformed = await browser.post(
            "/oauth/github/confirm",
            content=b"\xff",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert malformed.status_code == 400
        assert malformed.headers["cache-control"] == "no-store"
        assert malformed.headers["x-frame-options"] == "DENY"
        assert malformed.headers["referrer-policy"] == "no-referrer"
        assert 'gh-repo-name">one' in page.text
        assert 'gh-repo-name">two' not in page.text
        assert "Select all" in page.text
        assert "onclick=" not in page.text
        assert 'id="select-all-repos"' in page.text
        assert "All repos in this org" not in page.text
        assert 'name="repo" value="101"' in page.text
        assert 'name="repo" value="101" checked' not in page.text
        cancelled = await browser.get(
            "/oauth/github/confirm", params={"state": state, "cancel": "1"}
        )
        assert "Nothing was connected." in cancelled.text
        assert "You can close this tab." in cancelled.text
        assert (
            await browser.get("/oauth/github/confirm", params={"state": state})
        ).status_code == 400
        restart = await browser.get(f"/oauth/github/connect/{invitation_token}")
        assert restart.status_code == 307
        state = parse_qs(urlparse(restart.headers["location"]).query)["state"][0]
        assert (
            await browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        ).status_code == 307
        installations_available = False
        install_page = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "Install Daimon on GitHub" in install_page.text
        assert "You'll choose which repos to connect after GitHub." in install_page.text
        approval_pending = True
        waiting = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "Waiting for GitHub approval" in waiting.text
        assert "A GitHub owner must approve Daimon." in waiting.text
        assert "Check again" in waiting.text
        assert waiting.text.count('class="gh-primary"') == 1
        approval_pending = False
        installations_available = True
        repo_admin = False
        no_managed = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "No repos available to connect" in no_managed.text
        assert "Copy link" in no_managed.text
        assert invitation_token in no_managed.text
        approval_pending = True
        waiting_with_other_install = await browser.get(
            "/oauth/github/confirm", params={"state": state}
        )
        assert "Waiting for GitHub approval" in waiting_with_other_install.text
        approval_pending = False
        repo_admin = True
        github_unavailable = True
        unavailable = await browser.get("/oauth/github/confirm", params={"state": state})
        assert unavailable.status_code == 502
        assert "Couldn't reach GitHub" in html.unescape(unavailable.text)
        assert "Try again" in unavailable.text
        github_unavailable = False
        repository_selection = "selected"
        repo_two_admin = True
        picker = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "Choose repos" in picker.text
        assert 'id="search-repos"' in picker.text
        assert 'type="hidden" name="repo"' not in picker.text
        assert 'name="repo" value="101" checked' not in picker.text
        assert 'name="repo" value="102" checked' not in picker.text
        assert 'name="access" value="write" checked' in picker.text
        empty = await browser.post("/oauth/github/confirm", data={"state": state})
        assert "Select at least one repo" in empty.text
        assert 'id="github-connect-form"' in empty.text
        spoof = await browser.get(
            "/oauth/github/setup", params={"state": state, "installation_id": "999999"}
        )
        assert spoof.status_code == 307
        assert "installation_id" not in spoof.headers["location"]
        repo_admin = False
        denied = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access": "write"}
        )
        assert denied.status_code == 403
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        repo_admin = True
        repo_two_admin = True
        async with sessionmaker.begin() as session:
            await set_role(session, account_id, Role.USER)
        demoted = await browser.post("/oauth/github/confirm", data={"state": state, "repo": "101"})
        assert demoted.status_code == 400
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        async with sessionmaker.begin() as session:
            await set_role(session, account_id, Role.ADMIN)
            await set_external(session, account_id, True)
        external = await browser.get("/oauth/github/confirm", params={"state": state})
        assert external.status_code == 400
        async with sessionmaker.begin() as session:
            await set_external(session, account_id, False)
            await set_role(session, account_id, Role.ADMIN)
        installation_available = False
        unavailable = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101"}
        )
        assert unavailable.status_code == 502
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        installation_available = True
        confirmed = await browser.post(
            "/oauth/github/confirm",
            data={
                "state": state,
                "repo": ["101", "102"],
                "access": "read",
            },
        )
        assert confirmed.status_code == 200 and "Connected 2 repos" in confirmed.text
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "read", 102: "read"}
            assert all(repo.installation_id == 77 for repo in repos)
            installation = await github_app_installations.get(session, installation_id=77)
            assert installation is not None
            assert installation.app == "github_app"
            assert installation.account_login == "example"
            assert installation.account_id == 55
            assert installation.account_type == "Organization"
            assert installation.repository_selection == "selected"
            assert set(installation.repo_full_names) == {"example/one", "example/two"}
            assert (
                await github_app_installations.get_for_repo(session, repo_full_name="example/one")
                is None
            )
            events = await list_events(session, tenant_id=tenant_id)
            assert len(events) == 1
            assert events[0].operation == "github_connect"
            assert events[0].github_repo_ids == [101, 102]
        github_unavailable = True
        reused = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access": "write"}
        )
        assert reused.status_code == 200
        assert (
            "Already connected: 2 repos." in reused.text
            and "You can close this tab." in reused.text
        )
        async with sessionmaker.begin() as session:
            notice = await claim_next(session, platform="discord", now=datetime.now(UTC))
            assert notice is not None
            assert notice.text == "Connected example/one, example/two, Read only. Ready."
            await settle(session, notice=notice, delivered=True, now=datetime.now(UTC))
            assert await claim_next(session, platform="discord", now=datetime.now(UTC)) is None
        forged = await browser.post(
            "/oauth/github/confirm",
            data={
                "state": "not-this-flow",
                "invitation": github_connect.digest(invitation_token),
                "receipt": "invalid",
            },
        )
        assert forged.status_code == 400
        github_unavailable = False
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "read", 102: "read"}
            assert len(await list_events(session, tenant_id=tenant_id)) == 1
        used_link = await browser.get(f"/oauth/github/connect/{invitation_token}")
        assert used_link.status_code == 200
        assert (
            "Already connected: 2 repos." in used_link.text
            and "You can close this tab." in used_link.text
        )
        async with sessionmaker.begin() as session:
            expired_token = await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                requester_label="Alex",
            )
            await github_connect.expire_pending_invitation(
                session, tenant_id=tenant_id, requester_account_id=account_id
            )
        expired = await browser.get(f"/oauth/github/connect/{expired_token}")
        assert expired.status_code == 400
        assert "This link has expired." in expired.text
        assert "Ask Alex for a new one." in expired.text
        async with sessionmaker.begin() as session:
            default_token = await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                requester_label="Alex",
            )
        default_start = await browser.get(f"/oauth/github/connect/{default_token}")
        default_state = parse_qs(urlparse(default_start.headers["location"]).query)["state"][0]
        assert (
            await browser.get(
                "/oauth/github/callback", params={"state": default_state, "code": "code"}
            )
        ).status_code == 307
        default_form = await browser.get("/oauth/github/confirm", params={"state": default_state})
        assert 'name="access" value="write" checked' in default_form.text
        signature_match = re.search(r'name="receipt" value="([a-f0-9]+)"', default_form.text)
        assert signature_match is not None
        signed_form = {
            "state": default_state,
            "repo": ["101", "102"],
            "invitation": github_connect.digest(default_token),
            "receipt": signature_match.group(1),
        }
        first, second = await asyncio.gather(
            browser.post("/oauth/github/confirm", data=signed_form),
            browser.post("/oauth/github/confirm", data=signed_form),
        )
        assert first.status_code == second.status_code == 200
        assert sorted(
            [
                "already" if "Already connected: 2 repos." in response.text else "connected"
                for response in (first, second)
            ]
        ) == ["already", "connected"]
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "write", 102: "write"}
            assert len(await list_events(session, tenant_id=tenant_id)) == 2
        async with sessionmaker.begin() as session:
            await github_connect.delete_expired_flows(
                session, now=datetime.now(UTC) + timedelta(days=8)
            )
        replay_after_cleanup = await browser.post("/oauth/github/confirm", data=signed_form)
        assert replay_after_cleanup.status_code == 200
        assert "Already connected: 2 repos." in replay_after_cleanup.text
        async with sessionmaker.begin() as session:
            client = await make_account(session, tenant=tenant)
            await set_external(session, client.id, True)
            client_token = await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                requester_label="Alex",
            )
        client_start = await browser.get(f"/oauth/github/connect/{client_token}")
        client_state = parse_qs(urlparse(client_start.headers["location"]).query)["state"][0]
        assert (
            await browser.get(
                "/oauth/github/callback", params={"state": client_state, "code": "code"}
            )
        ).status_code == 307
        client_form = await browser.get("/oauth/github/confirm", params={"state": client_state})
        assert "Clients can see what connected agents share." in client_form.text
        assert 'class="web-icon web-icon--triangle-alert"' in client_form.text
        assert "Pick only the repos" not in client_form.text
        assert 'id="select-all-repos"' in client_form.text
        assert 'name="repo" value="101" checked' not in client_form.text
        assert 'type="hidden" name="repo"' not in client_form.text
    assert any(request.url.path == "/user/installations" for request in requests)
    assert all(request.url.path != "/user/memberships/orgs" for request in requests)
    assert sum(request.url.path == "/applications/client/token" for request in requests) == 4
