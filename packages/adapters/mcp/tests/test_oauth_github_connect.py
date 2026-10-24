"""Browser GitHub connection routes with a mocked GitHub API."""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import pytest_asyncio
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
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_installation_reconciliation import get as get_reconciliation
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

_INSTALLATION_WRITE = {
    "metadata": "read",
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
}


@pytest.fixture(autouse=True)
def _existing_live_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing route scenarios exercise GitHub; live platform cases override this stub."""
    monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=True))


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
            repos=(oauth_github._Repo(index, index, index, f"{login}/repo", "admin"),),
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


@pytest.mark.parametrize(
    ("requested_repo", "already_added", "expected"),
    [
        ("COMPANY/REPO", frozenset(), 'value="1" checked>'),
        ("company/unknown", frozenset(), 'value="1">'),
        ("company/private", frozenset(), 'value="1">'),
        (None, frozenset(), 'value="1">'),
        ("company/repo", frozenset({1}), 'value="1" checked disabled>'),
    ],
)
def test_picker_preselects_only_available_requested_repo(
    requested_repo: str | None, already_added: frozenset[int], expected: str
) -> None:
    installation = oauth_github._Installation(
        id=1,
        owner_id=1,
        owner_login="company",
        repository_selection="selected",
        repos=(
            oauth_github._Repo(1, 1, 1, "company/repo", "admin"),
            oauth_github._Repo(2, 1, 1, "company/private", None),
        ),
        owner_type="Organization",
    )
    page = oauth_github._confirmation_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        cancel_url="https://discord.com",
        installations=[installation],
        platform="discord",
        workspace="Test Server",
        agent_name="Test Agent",
        already_added=already_added,
        requested_repo=requested_repo,
    )
    assert expected in page.body.decode()
    assert 'value="2"' not in page.body.decode()


@pytest.mark.parametrize(
    ("requested_repo", "already_granted", "expected"),
    [
        ("ACME/DATA", False, True),
        ("acme/other", False, False),
        (None, False, False),
        ("ACME/DATA", True, False),
    ],
)
def test_agent_editor_preselects_requested_repo_in_add_dialog(
    requested_repo: str | None, already_granted: bool, expected: bool
) -> None:
    installation = oauth_github._Installation(
        id=1,
        owner_id=1,
        owner_login="acme",
        repository_selection="selected",
        repos=(oauth_github._Repo(1, 1, 1, "acme/data", "admin"),),
    )
    grants = (
        [
            github_access.AgentRepo(
                repo_id=1,
                full_name="acme/data",
                scope="agent",
                max_access="read",
                baseline_access="read",
                ceiling_access="read",
                staged=False,
                status="active",
                added_by_account_id=None,
                added_at=datetime.now(UTC),
                granted_by_account_id=None,
                granted_at=datetime.now(UTC),
            )
        ]
        if already_granted
        else []
    )
    page = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="Test Agent",
        workspace="Test Server",
        platform="discord",
        grants=grants,
        installations=[installation],
        snapshot="snapshot",
        cancel_url="https://discord.com",
        install_url="https://github.com/apps/test/installations/new",
        requested_repo=requested_repo,
    )
    dialog = page.body.decode().split('<dialog id="repo-picker"', 1)[1].split("</dialog>", 1)[0]
    assert ('class="gh-picker-toggle" type="checkbox" checked>' in dialog) is expected
    assert ('class="gh-picker-choice" data-repo-id="1"' in dialog) is not already_granted


def test_done_page_names_github_and_discord_with_marks() -> None:
    page = oauth_github._done_page(
        count=2,
        platform="discord",
        external_id="123",
        requester_label="Alex",
        same_person=True,
        agent_name="ResearchBot",
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
    assert "A team&lt;one&gt; owner must approve Daimon." in text
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
        assert "No changes saved." in cancelled.text
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
        assert "No changes saved." in repeated.text
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
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_requested_repo_picker_posts_only_selected_repo(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(
            session, id=tenant_id, workspace_id="workspace", platform=platform
        )
        account = await make_account(session, tenant=tenant, id=account_id)
        await make_platform_principal(
            session, platform=platform, external_id="123", tenant=tenant, account=account
        )
        await set_role(session, account_id, Role.ADMIN)
        invitation_token = await github_connect.mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
            requester_platform_user_id="123",
            requested_repo="EXAMPLE/TWO",
            origin_platform=platform,
        )

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        if request.url.path == "/applications/client/token":
            return httpx.Response(204)
        if request.url.path == "/app/installations/77":
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "account": {"id": 55, "login": "example", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": _INSTALLATION_WRITE,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 77,
                            "account": {"id": 55, "login": "example", "type": "Organization"},
                            "repository_selection": "selected",
                        }
                    ]
                },
            )
        if request.url.path == "/user/installations/77/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": repo_id,
                            "owner": {"id": 55},
                            "full_name": name,
                            "permissions": {"admin": True},
                        }
                        for repo_id, name in [(101, "example/one"), (102, "example/two")]
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    key = Fernet.generate_key().decode()
    settings = _settings(key)
    monkeypatch.setattr(oauth_github, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=settings,
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
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
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback_response = await browser.get(
            "/oauth/github/callback", params={"state": state, "code": "code"}
        )
        assert callback_response.status_code == 307
        picker = await browser.get("/oauth/github/confirm", params={"state": state})
        assert 'name="repo" value="101" checked' not in picker.text
        assert 'name="repo" value="102" checked>' in picker.text
        assert 'name="access" value="write" checked' in picker.text
        async with committing_sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        live = AsyncMock(
            side_effect=[False, oauth_github.AdminAttestationUnavailable("lookup failed"), True]
        )
        monkeypatch.setattr(oauth_github, "_attest_native_admin", live)
        for expected_status in (403, 503):
            refused = await browser.post(
                "/oauth/github/confirm", data={"state": state, "repo": "102", "access": "read"}
            )
            assert refused.status_code == expected_status
            async with committing_sessionmaker() as session:
                assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        submitted = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "102", "access": "read"}
        )
        assert submitted.status_code == 200
        assert live.await_count == 3
        async with committing_sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert [(repo.repo_id, repo.max_access) for repo in repos] == [(102, "write")]


@pytest.mark.asyncio
async def test_connection_happy_path_and_rechecks(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="workspace")
        account = await make_account(session, tenant=tenant, id=account_id)
        await make_platform_principal(
            session, platform="discord", external_id="123", tenant=tenant, account=account
        )
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
                    "permissions": _INSTALLATION_WRITE,
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
                            "permissions": {"admin": repo_two_admin},
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
        assert "No changes saved." in refused_oauth.text
        assert "Return to Discord when you are ready." in refused_oauth.text
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
        assert "Choose GitHub repos" in page.text
        assert "0 selected" in page.text
        assert 'id="github-connect-form"' in page.text
        assert 'const doing = "Connecting";' in page.text
        assert "submit.disabled = true" in page.text
        assert "if (connecting || !boxes.some(box => box.checked))" in page.text
        assert 'name="access" value="write" checked' in page.text
        assert 'class="web-icon web-icon--search"' in page.text
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
        assert "No changes saved." in cancelled.text
        assert "Return to Discord when you are ready." in cancelled.text
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
        assert "Connect GitHub" in install_page.text
        assert "Choose the GitHub organization and repos you want to connect." in install_page.text
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
        assert "Copy setup link" in no_managed.text
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
        assert "Choose GitHub repos" in picker.text
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
        assert confirmed.status_code == 200 and "2 repos connected." in confirmed.text
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "write", 102: "write"}
            assert all(repo.installation_id == 77 for repo in repos)
            installation = await github_app_installations.get(session, installation_id=77)
            assert installation is not None
            assert installation.app == "github_app"
            assert installation.account_login == "example"
            assert installation.account_id == 55
            assert installation.account_type == "Organization"
            assert installation.repository_selection == "selected"
            assert set(installation.repo_full_names) == {"example/one", "example/two"}
            refresh = await get_reconciliation(session, installation_id=77)
            assert refresh is not None and refresh.generation == 1
            assert (
                await github_app_installations.get_for_repo(session, repo_full_name="example/one")
                is not None
            ), "the confirmed set applies until the first GitHub refresh"
            events = await list_events(session, tenant_id=tenant_id)
            assert len(events) == 1
            assert events[0].operation == "github_connect"
            assert events[0].github_repo_ids == [101, 102]
        github_unavailable = True
        reused = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access": "write"}
        )
        assert reused.status_code == 200
        assert "Already connected" in reused.text and "You can close this tab." in reused.text
        async with sessionmaker.begin() as session:
            notice = await claim_next(session, platform="discord", now=datetime.now(UTC))
            assert notice is not None
            assert notice.text == "Connected example/one, example/two. Ready."
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
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "write", 102: "write"}
            assert len(await list_events(session, tenant_id=tenant_id)) == 1
        used_link = await browser.get(f"/oauth/github/connect/{invitation_token}")
        assert used_link.status_code == 200
        assert "Already connected" in used_link.text and "You can close this tab." in used_link.text
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
        assert "Ask Alex for a new link." in expired.text
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
                "already" if "Already connected" in response.text else "connected"
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
        assert "Already connected" in replay_after_cleanup.text
        async with sessionmaker.begin() as session:
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
        assert "How this works" in client_form.text
        assert "Pick only the repos" not in client_form.text
        assert 'id="select-all-repos"' in client_form.text
        assert 'name="repo" value="101" checked' not in client_form.text
        assert 'type="hidden" name="repo"' not in client_form.text
    assert any(request.url.path == "/user/installations" for request in requests)
    assert all(request.url.path != "/user/memberships/orgs" for request in requests)
    assert sum(request.url.path == "/applications/client/token" for request in requests) == 4


def _agent_picker(
    *, already_added: frozenset[int] = frozenset(), needed: dict[int, bool] | None = None
) -> str:
    repos = tuple(
        oauth_github._Repo(index, 5, 7, f"lab/repo-{index}", "admin") for index in (1, 2, 3)
    )
    installation = oauth_github._Installation(
        id=7, owner_id=5, owner_login="lab", repository_selection="all", repos=repos
    )
    return oauth_github._confirmation_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        cancel_url="https://discord.com",
        installations=[installation],
        platform="discord",
        workspace="Test Server",
        agent_name="ResearchBot",
        already_added=already_added,
        needed=needed,
    ).body.decode()


def test_agent_picker_names_the_agent_and_who_can_use_it() -> None:
    body = _agent_picker()
    assert "<h1>Choose GitHub repos</h1>" in body
    assert "Connect repos to ResearchBot." in body
    assert "Anyone who talks to ResearchBot can ask it to read and change them." in body
    assert "Server: Test Server" in body
    assert 'name="access" value="write" checked' in body
    assert '<span class="gh-button-label">Connect selected repos</span>' in body
    assert " · " not in body


def test_agent_picker_ticks_and_greys_repos_already_added() -> None:
    body = _agent_picker(already_added=frozenset({2}))
    assert '<input type="checkbox" name="repo" value="2" checked disabled>' in body
    assert '<input type="checkbox" name="repo" value="1">' in body
    assert body.count("Already connected") == 1
    # Only new ticks count toward the button and are sent.
    assert "const boxes = all.filter(box => !box.disabled);" in body


def test_agent_picker_offers_what_the_agent_still_needs() -> None:
    body = _agent_picker(already_added=frozenset({1}), needed={2: True, 3: False})
    assert '<input type="checkbox" name="repo" value="1" checked disabled>' in body
    assert '<input type="checkbox" name="repo" value="2" checked>' in body
    assert '<input type="checkbox" name="repo" value="3" checked>' in body
    assert 'repo-2</span><span class="gh-added">Needs write</span>' in body
    assert 'repo-3</span><span class="gh-added">Needed</span>' in body
    assert body.count("Already connected") == 1


def test_editor_stages_remove_undo_and_summarizes_every_change() -> None:
    now = datetime.now(UTC)
    grants = [
        github_access.AgentRepo(
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
            is_working_repo=repo_id == 1,
        )
        for repo_id, name in ((1, "lab/visible"), (2, "lab/hidden"))
    ]
    install = oauth_github._Installation(
        id=7,
        owner_id=5,
        owner_login="lab",
        repository_selection="selected",
        repos=(
            oauth_github._Repo(1, 5, 7, "lab/visible", "admin"),
            oauth_github._Repo(2, 5, 7, "lab/hidden", None),
            oauth_github._Repo(3, 5, 7, "lab/new", "admin"),
        ),
    )
    body = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="ResearchBot",
        workspace="Test Server",
        platform="discord",
        grants=grants,
        installations=[install],
        snapshot="snapshot",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/sample-app/installations/new",
        missing_names=("lab/hidden",),
    ).body.decode()
    assert "lab/hidden" not in body
    assert "Hidden repo" in body
    assert 'data-action="remove" aria-label="Remove Hidden repo"' in body
    assert 'data-action="remove" aria-label="Remove lab/visible"' in body
    assert 'data-action="undo" aria-label="Undo removal of lab/visible"' in body
    assert 'isRemoved ? "− To remove"' in body
    assert "removed.delete(id);" in body
    assert 'parts.length ? "Not saved yet" : "No changes to save"' in body
    assert 'parts.push("change working repo")' in body
    assert 'id="repo-count"' not in body
    assert "cancel.hidden = !!parts.length;" in body
    assert "save.hidden = !parts.length;" in body
    assert "Change working repo" in body
    assert 'data-action="working"' not in body
    assert "✓ Added" in body


def test_editor_hides_unlisted_private_grants_even_under_truncated_owner() -> None:
    now = datetime.now(UTC)
    grants = [
        github_access.AgentRepo(
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
            is_working_repo=repo_id == 2,
        )
        for repo_id, name in (
            (1, "big/visible"),
            (2, "big/private-other-person"),
            (3, "lost/stale"),
        )
    ]
    install = oauth_github._Installation(
        id=7,
        owner_id=5,
        owner_login="big",
        repository_selection="selected",
        repos=(oauth_github._Repo(1, 5, 7, "big/visible", "admin"),),
        truncated=True,
    )
    body = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="ResearchBot",
        workspace="Test Server",
        platform="discord",
        grants=grants,
        installations=[install],
        snapshot="snapshot",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/sample-app/installations/new",
    ).body.decode()
    past_cap_row = body.split('data-repo-id="2"', 1)[1].split("</article>", 1)[0]
    stale_row = body.split('data-repo-id="3"', 1)[1].split("</article>", 1)[0]
    assert '<strong id="working-card-name">Hidden repo</strong>' in body
    assert '<strong class="gh-row-name">Hidden repo</strong>' in past_cap_row
    assert 'aria-label="Remove Hidden repo"' in past_cap_row
    assert "Past the 2,000-repo limit, so you can only remove it here." in past_cap_row
    assert 'gh-working-action"' not in past_cap_row
    assert '<strong class="gh-row-name">Hidden repo</strong>' in stale_row
    assert "You can't see this repo on GitHub." in stale_row
    assert "big/private-other-person" not in body
    assert "lost/stale" not in body
    saved = oauth_github._saved_agent_page("ResearchBot", grants, visible_ids={1}).body.decode()
    assert "big/private-other-person" not in saved
    assert "lost/stale" not in saved
    assert saved.count("Hidden repo") == 2
    assert 'data-repo-id="1"' not in body.split('<dialog id="repo-picker"', 1)[1]


def test_pages_only_offer_actions_with_a_route() -> None:
    no_link = oauth_github._no_repos_page("", "https://github.com/apps/sample/installations/new")
    assert 'id="copy-link"' not in no_link.body.decode()
    assert "Open GitHub setup" in no_link.body.decode()
    no_retry = oauth_github._error("Could not connect repos")
    assert "Try again</a>" not in no_retry.body.decode()
    assert "Ask the requester for a new setup link." in no_retry.body.decode()
    slack = oauth_github._cancelled_page(oauth_github._back_to_chat("slack", "workspace"))
    assert "Return to Discord" not in slack.body.decode()
    assert "In Slack, run /github" in slack.body.decode()


def test_teams_pages_never_send_people_to_slack_or_discord() -> None:
    """A Teams requester has no /github command and no Discord server to go back to."""
    back = oauth_github._back_to_chat("teams", "entra-tenant")
    pages = {
        "cancelled": oauth_github._cancelled_page(back),
        "already connected": oauth_github._already_connected_page(
            2, back, agent_name="ResearchBot"
        ),
        "done": oauth_github._done_page(
            count=2,
            platform="teams",
            external_id="entra-tenant",
            requester_label="Alex",
            same_person=True,
            agent_name=None,
        ),
    }
    for name, page in pages.items():
        body = page.body.decode()
        assert "Slack" not in body and "Discord" not in body, f"{name} page names another platform"
    assert "You can close this tab." in pages["cancelled"].body.decode(), "cancel should close"
    assert "In Teams, ask me" in pages["done"].body.decode(), "done should point back to Teams"


def _agent_done(**changes: object) -> str:
    args: dict[str, object] = {
        "count": 2,
        "platform": "slack",
        "external_id": "U1",
        "requester_label": "Alex",
        "same_person": True,
        "agent_name": "ResearchBot",
    }
    args.update(changes)
    return oauth_github._done_page(**args).body.decode()  # type: ignore[arg-type]


def test_agent_done_page_says_connected_and_close_tab() -> None:
    body = _agent_done()
    assert "2 repos connected to ResearchBot." in body
    assert "You can close this tab." in body
    assert "old GitHub token" not in body


@pytest.mark.asyncio
async def test_agent_editor_saves_explicit_changes_and_rejects_stale_page(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import DeploymentDefault

    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_editor")
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="editor-workspace")
        account = await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        await make_platform_principal(
            session, platform="discord", external_id="editor", tenant=tenant, account=account
        )

    admin_ids = {101, 102, 103}

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        if path == "/applications/client/token":
            return httpx.Response(204)
        if path == "/user":
            return httpx.Response(200, json={"id": 17})
        if path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 77,
                            "account": {"id": 55, "login": "editor", "type": "User"},
                            "repository_selection": "selected",
                        }
                    ]
                },
            )
        if path == "/user/installations/77/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": repo_id,
                            "owner": {"id": 55},
                            "full_name": f"editor/repo-{repo_id}",
                            "permissions": {"admin": repo_id in admin_ids},
                        }
                        for repo_id in (101, 102, 103)
                    ]
                },
            )
        if path == "/app/installations/77":
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "account": {"id": 55, "login": "editor", "type": "User"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": _INSTALLATION_WRITE,
                },
            )
        raise AssertionError(path)

    async def find_agent(_client: object, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> object:
        return SimpleNamespace(name="Editor", metadata={})

    monkeypatch.setattr(oauth_github, "find_agent_by_derived_uuid", find_agent)
    monkeypatch.setattr(oauth_github, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")
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

        async def page(*, operator: bool = False) -> tuple[str, dict[str, str], str]:
            async with sessionmaker.begin() as session:
                invitation = await github_connect.mint_invitation(
                    session,
                    tenant_id=tenant_id,
                    requester_account_id=account_id,
                    requester_label="Admin",
                    requester_platform_user_id="editor",
                    agent_id=agent_id,
                    agent_name="Editor",
                    agent_ma_id=None if operator else "ag_editor",
                    agent_manager_verified=True,
                    operator_issued=operator,
                    origin_platform="discord",
                )
            start = await browser.get(f"/oauth/github/connect/{invitation}")
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            assert (
                await browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
            ).status_code == 307
            rendered = await browser.get("/oauth/github/confirm", params={"state": state})
            assert rendered.status_code == 200
            hidden = {
                name: html.unescape(value)
                for name, value in re.findall(
                    r'<input type="hidden" name="([^"]+)" value="([^"]*)">', rendered.text
                )
            }
            return state, hidden, rendered.text

        async def save(hidden: dict[str, str], **choices: object) -> httpx.Response:
            return await browser.post(
                "/oauth/github/confirm",
                data={**hidden, "access": "write", "working": "keep", **choices},
            )

        state, hidden, rendered = await page()
        assert "No repos connected yet" in rendered
        assert '<dialog id="repo-picker"' in rendered
        assert '<noscript><section class="gh-fallback">' in rendered
        assert (await save(hidden)).status_code == 200  # No-op keeps the invitation.
        added = await save(hidden, repo=["101", "102"])
        assert added.status_code == 200
        assert "Editor can read and change these 2 repos." in added.text
        async with sessionmaker() as session:
            grants = await github_access.list_agent_repos(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        assert {repo.repo_id: repo.ceiling_access for repo in grants} == {
            101: "write",
            102: "write",
        }
        replay = await save(hidden, repo=["101", "102"])
        assert (
            replay.status_code == 200 and "Editor can read and change these 2 repos." in replay.text
        )

        state, hidden, rendered = await page()
        assert "Can read and change" in rendered
        assert "Read only" not in rendered and "Read and write" not in rendered
        assert (await save(hidden, repo="101")).status_code == 403
        assert (await save(hidden, repo="103", access_103="admin")).status_code == 400
        assert (await save(hidden, repo="103", access_103=["read", "write"])).status_code == 400
        working = await save(hidden, working="101")
        assert (
            working.status_code == 200
            and "Editor can read and change these 2 repos." in working.text
        )

        admin_ids.clear()
        with monkeypatch.context() as pending_patch:
            pending_patch.setattr(
                oauth_github,
                "has_pending_installation_request",
                AsyncMock(return_value=oauth_github._PendingInstallationRequest(True, "editor")),
            )
            _state, _hidden, unavailable = await page()
        assert "An editor owner must approve Daimon" in unavailable
        assert "editor/repo-101" not in unavailable and "editor/repo-102" not in unavailable
        assert "Hidden repo" in unavailable
        assert "Open GitHub setup" in unavailable
        admin_ids.update({101, 102, 103})

        state, hidden, rendered = await page()
        admin_ids.remove(102)
        denied = await save(hidden, remove="102")
        assert denied.status_code == 409
        admin_ids.add(102)
        removed = await save(hidden, remove="102")
        assert removed.status_code == 200
        assert "repo-102" not in removed.text

        state, hidden, rendered = await page()
        mixed = await save(hidden, repo="103", remove="101", working="103")
        assert mixed.status_code == 200
        assert "repo-101" not in mixed.text
        assert "Editor can read and change this repo." in mixed.text

        state, hidden, rendered = await page()
        cleared = await save(hidden, working="clear")
        assert cleared.status_code == 200
        assert "Editor can read and change this repo." in cleared.text

        state, hidden, rendered = await page()
        async with sessionmaker.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=103,
                baseline_access="read",
                ceiling_access="read",
                granted_by_account_id=account_id,
                mount_path="/workspace/editor/repo-103-stale",
            )
        stale = await save(hidden, remove="103")
        assert stale.status_code == 409
        async with sessionmaker() as session:
            status, _ = await github_connect.invitation_status(session, hidden["invitation"])
            assert status == "active"
            grants = await github_access.list_agent_repos(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
            assert [repo.repo_id for repo in grants] == [103]

        state, hidden, rendered = await page()
        original_set_working = github_access.set_working_repo

        async def reject_working(*_args: object, **_kwargs: object) -> None:
            raise ValueError("forced rollback")

        monkeypatch.setattr(github_access, "set_working_repo", reject_working)
        rolled_back = await save(hidden, repo="102", working="102")
        assert rolled_back.status_code == 400
        monkeypatch.setattr(github_access, "set_working_repo", original_set_working)
        async with sessionmaker() as session:
            status, _ = await github_connect.invitation_status(session, hidden["invitation"])
            grants = await github_access.list_agent_repos(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        assert status == "active"
        assert [repo.repo_id for repo in grants] == [103]

        # A chat write between the display read and signature read must not
        # sign a newer state than the page displayed.
        original_list = oauth_github.list_agent_repos
        injected = False
        reads = 0

        async def list_then_chat_write(
            session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
        ) -> list[github_access.AgentRepo]:
            nonlocal injected, reads
            rows = await original_list(session, tenant_id=tenant_id, agent_id=agent_id)
            reads += 1
            if reads == 2:
                injected = True
                async with sessionmaker.begin() as writer:
                    await github_access.stage_grant(
                        writer,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=103,
                        baseline_access="read",
                        ceiling_access="read",
                        granted_by_account_id=account_id,
                        mount_path="/workspace/editor/repo-103-later",
                    )
            return rows

        monkeypatch.setattr(oauth_github, "list_agent_repos", list_then_chat_write)
        state, hidden, rendered = await page()
        monkeypatch.setattr(oauth_github, "list_agent_repos", original_list)
        assert injected and "repo-103" in rendered
        assert (await save(hidden, remove="103")).status_code == 409

        _state, hidden, _rendered = await page()
        async with sessionmaker() as session:
            before = len(
                [
                    event
                    for event in await list_events(session, tenant_id=tenant_id)
                    if event.reason == "agent repos saved"
                ]
            )
        first, second = await asyncio.gather(save(hidden, remove="103"), save(hidden, remove="103"))
        assert sorted((first.status_code, second.status_code)) in ([200, 200], [200, 409])
        async with sessionmaker() as session:
            after = len(
                [
                    event
                    for event in await list_events(session, tenant_id=tenant_id)
                    if event.reason == "agent repos saved"
                ]
            )
            grants = await github_access.list_agent_repos(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        assert after == before + 1
        assert grants == []

        # CLI operator links omit agent_ma_id; they remain restricted to a
        # current tenant admin and still open this editor.
        _state, hidden, rendered = await page(operator=True)
        assert "Editor&#x27;s GitHub repos" in rendered
        operator_live = AsyncMock(
            side_effect=AssertionError("operator invitation requested live admin lookup")
        )
        monkeypatch.setattr(oauth_github, "_attest_native_admin", operator_live)
        assert (await save(hidden, repo="101")).status_code == 200
        operator_live.assert_not_awaited()

        async with sessionmaker.begin() as session:
            operator_token = await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                requester_label="Admin",
                requester_platform_user_id="editor",
                agent_id=agent_id,
                agent_name="Editor",
                operator_issued=True,
            )
            await set_role(session, account_id, Role.USER)
        revoked_receipt = await save(hidden, repo="101")
        assert revoked_receipt.status_code == 403
        assert "editor/repo-101" not in revoked_receipt.text
        start = await browser.get(f"/oauth/github/connect/{operator_token}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        await browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        denied_operator = await browser.get("/oauth/github/confirm", params={"state": state})
        assert denied_operator.status_code == 403


# --- Agent editor request limits, retry and deadline -------------------------


@dataclass
class _EditorEnv:
    """An agent editor wired to a scripted GitHub, with switches to break it."""

    browser: httpx.AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    github_calls: list[str] = field(default_factory=list)
    listing_down: bool = False
    details_down: bool = False
    listing_delay: float = 0.0
    multi_owners: bool = False
    failed_owner_id: int | None = None

    async def open_editor(self) -> tuple[str, dict[str, str]]:
        """Run the connect flow and return the state plus the editor's hidden fields."""
        account_id = await self._account_id()
        async with self.sessionmaker.begin() as session:
            invitation = await github_connect.mint_invitation(
                session,
                tenant_id=self.tenant_id,
                requester_account_id=account_id,
                requester_label="Admin",
                requester_platform_user_id="editor",
                agent_id=self.agent_id,
                agent_name="Editor",
                agent_ma_id="ag_editor",
                agent_manager_verified=True,
                operator_issued=False,
                origin_platform="discord",
            )
        start = await self.browser.get(f"/oauth/github/connect/{invitation}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        await self.browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        rendered = await self.browser.get("/oauth/github/confirm", params={"state": state})
        assert rendered.status_code == 200
        return state, _hidden_fields(rendered.text)

    async def _account_id(self) -> uuid.UUID:
        async with self.sessionmaker() as session:
            row = await session.execute(
                text("SELECT id FROM accounts WHERE tenant_id = :tenant"),
                {"tenant": self.tenant_id},
            )
            return row.scalar_one()

    async def saved_repo_ids(self) -> list[int]:
        async with self.sessionmaker() as session:
            grants = await github_access.list_agent_repos(
                session, tenant_id=self.tenant_id, agent_id=self.agent_id
            )
        return sorted(grant.repo_id for grant in grants)


def _hidden_fields(page_html: str) -> dict[str, str]:
    return {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page_html
        )
    }


def _hidden_pairs(page_html: str) -> list[tuple[str, str]]:
    return [
        (name, html.unescape(value))
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page_html
        )
    ]


@pytest_asyncio.fixture
async def editor_env(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_EditorEnv]:
    from types import SimpleNamespace

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import DeploymentDefault

    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_editor")
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="limits-workspace")
        account = await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        await make_platform_principal(
            session, platform="discord", external_id="editor", tenant=tenant, account=account
        )

    env: _EditorEnv

    async def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        owners = (
            (
                (77, 55, "editor", "User"),
                (78, 56, "org-a", "Organization"),
                (79, 57, "org-b", "Organization"),
                (80, 58, "org-c", "Organization"),
            )
            if env.multi_owners
            else ((77, 55, "editor", "User"),)
        )
        if path.startswith(("/user/installations", "/app/installations")):
            env.github_calls.append(path)
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        if path == "/applications/client/token":
            return httpx.Response(204)
        if path == "/user":
            return httpx.Response(200, json={"id": 17})
        if path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": installation_id,
                            "account": {"id": owner_id, "login": login, "type": owner_type},
                            "repository_selection": "selected",
                        }
                        for installation_id, owner_id, login, owner_type in owners
                    ]
                },
            )
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            installation_id = int(path.split("/")[3])
            owner = next((item for item in owners if item[0] == installation_id), None)
            if owner is None:
                raise AssertionError(path)
            if env.listing_delay:
                await asyncio.sleep(env.listing_delay)
            if env.listing_down or env.failed_owner_id == installation_id:
                return httpx.Response(502)
            repo_ids = (
                (101, 102, 103)
                if installation_id == 77 and not env.multi_owners
                else (installation_id * 100 + 1,)
            )
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": repo_id,
                            "owner": {"id": owner[1]},
                            "full_name": f"{owner[2]}/repo-{repo_id}",
                            "permissions": {"admin": True},
                        }
                        for repo_id in repo_ids
                    ]
                },
            )
        if path.startswith("/app/installations/"):
            installation_id = int(path.split("/")[3])
            owner = next((item for item in owners if item[0] == installation_id), None)
            if owner is None:
                raise AssertionError(path)
            if env.details_down:
                return httpx.Response(502)
            return httpx.Response(
                200,
                json={
                    "id": installation_id,
                    "account": {"id": owner[1], "login": owner[2], "type": owner[3]},
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": _INSTALLATION_WRITE,
                },
            )
        raise AssertionError(path)

    async def find_agent(_client: object, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> object:
        return SimpleNamespace(name="Editor", metadata={})

    monkeypatch.setattr(oauth_github, "find_agent_by_derived_uuid", find_agent)
    monkeypatch.setattr(oauth_github, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")
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
        env = _EditorEnv(browser, sessionmaker, tenant_id, agent_id)
        yield env


async def _submit(
    browser: httpx.AsyncClient, url: str, pairs: list[tuple[str, str]]
) -> httpx.Response:
    """Post a form the way a browser does, repeating names that appear more than once."""
    return await browser.post(
        url,
        content=urlencode(pairs),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


async def _no_github_calls_after(env: _EditorEnv, marker: int) -> None:
    assert env.github_calls[marker:] == []
    assert await env.saved_repo_ids() == []


@pytest.mark.asyncio
async def test_editor_add_preserves_unselected_stale_grant(editor_env: _EditorEnv) -> None:
    env = editor_env
    env.multi_owners = True
    _, hidden = await env.open_editor()
    first = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "write", "working": "keep", "repo": "7801"},
    )
    assert first.status_code == 200
    assert await env.saved_repo_ids() == [7801]
    async with env.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE tenant_github_repos SET status = 'revoked' "
                "WHERE tenant_id = :tenant_id AND repo_id = 7801"
            ),
            {"tenant_id": env.tenant_id},
        )
    env.failed_owner_id = 78
    _, hidden = await env.open_editor()
    added = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "write", "working": "keep", "repo": "7701"},
    )
    assert added.status_code == 200
    assert await env.saved_repo_ids() == [7701, 7801]


@pytest.mark.asyncio
async def test_editor_mixed_owner_admission_error_rolls_back(
    editor_env: _EditorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = editor_env
    env.multi_owners = True
    _, hidden = await env.open_editor()
    original_budget = github_access.require_projected_grant_budget

    def reject_second_owner(
        repo_ids_by_installation: dict[int, set[int]],
        *,
        any_read_only: bool,
        has_working_repo: bool,
        agent_name: str,
    ) -> None:
        if len(repo_ids_by_installation) > 1:
            raise github_access.InstallationLimitError("forced admission failure")
        original_budget(
            repo_ids_by_installation,
            any_read_only=any_read_only,
            has_working_repo=has_working_repo,
            agent_name=agent_name,
        )

    monkeypatch.setattr(github_access, "require_projected_grant_budget", reject_second_owner)
    refused = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "write", "working": "keep", "repo": ["7701", "7801"]},
    )
    assert refused.status_code == 400
    assert "forced admission failure" in refused.text
    assert await env.saved_repo_ids() == []
    async with env.sessionmaker() as session:
        status, _ = await github_connect.invitation_status(session, hidden["invitation"])
        for repo_id in (7701, 7801):
            assert (
                await github_access.repo_for_agent(
                    session, tenant_id=env.tenant_id, agent_id=env.agent_id, repo_id=repo_id
                )
                is None
            )
    assert status == "active"


@pytest.mark.asyncio
async def test_editor_mixed_owner_save_is_atomic_and_binds_each_installation(
    editor_env: _EditorEnv,
) -> None:
    env = editor_env
    env.multi_owners = True
    _, hidden = await env.open_editor()
    first = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "write", "working": "keep", "repo": "8001"},
    )
    assert first.status_code == 200
    async with env.sessionmaker() as session:
        first_grants = await github_access.list_agent_repos(
            session, tenant_id=env.tenant_id, agent_id=env.agent_id
        )
        assert len(first_grants) == 1 and first_grants[0].is_working_repo
    _, hidden = await env.open_editor()
    mixed = await env.browser.post(
        "/oauth/github/confirm",
        data={
            **hidden,
            "access": "write",
            "working": "7701",
            "repo": ["7801", "7901", "7701"],
            "remove": "8001",
        },
    )
    assert mixed.status_code == 200
    assert "Editor can read and change these 3 repos." in mixed.text
    for owner in ("org-a", "org-b", "editor"):
        assert owner in mixed.text
    assert "org-c" not in mixed.text
    assert await env.saved_repo_ids() == [7701, 7801, 7901]
    async with env.sessionmaker() as session:
        for repo_id, installation_id in ((7701, 77), (7801, 78), (7901, 79)):
            repo = await github_access.repo_for_agent(
                session, tenant_id=env.tenant_id, agent_id=env.agent_id, repo_id=repo_id
            )
            assert repo is not None and repo.installation_id == installation_id

    _, hidden = await env.open_editor()
    env.failed_owner_id = 78
    stale = await env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "write", "working": "keep", "remove": "7901"},
    )
    assert stale.status_code == 409
    assert await env.saved_repo_ids() == [7701, 7801, 7901]
    state = hidden["state"]
    refreshed = await env.browser.get("/oauth/github/confirm", params={"state": state})
    assert refreshed.status_code == 200 and "Could not load repos." in refreshed.text
    removed = await env.browser.post(
        "/oauth/github/confirm",
        data={
            **_hidden_fields(refreshed.text),
            "access": "write",
            "working": "keep",
            "remove": "7801",
        },
    )
    assert removed.status_code == 200
    assert await env.saved_repo_ids() == [7701, 7901]
    env.failed_owner_id = None


@pytest.mark.asyncio
async def test_installation_lists_cap_each_owner_and_isolate_one_failure() -> None:
    pages: list[tuple[int, int]] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": installation_id,
                            "account": {
                                "id": installation_id,
                                "login": f"owner-{installation_id}",
                                "type": "Organization",
                            },
                        }
                        for installation_id in (1, 2, 3)
                    ]
                },
            )
        installation_id = int(path.split("/")[3])
        page = int(request.url.params["page"])
        pages.append((installation_id, page))
        if installation_id == 2:
            return httpx.Response(502)
        count = 100 if installation_id == 1 else 1
        return httpx.Response(
            200,
            json={
                "total_count": 4800 if installation_id == 1 else 1,
                "repositories": [
                    {
                        "id": installation_id * 10_000 + (page - 1) * 100 + index,
                        "owner": {"id": installation_id},
                        "full_name": f"owner-{installation_id}/repo-{page}-{index}",
                        "permissions": {"admin": True},
                    }
                    for index in range(count)
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(github_handler)) as client:
        owners = await oauth_github._installations(client, "test-token")
    assert len(owners[0].repos) == 2000
    assert owners[0].total == 4800 and owners[0].truncated
    assert owners[1].fetch_state == "error" and owners[1].repos == ()
    assert owners[2].fetch_state == "ok" and len(owners[2].repos) == 1
    assert (1, 20) in pages and (1, 21) not in pages


@pytest.mark.asyncio
async def test_confirm_refuses_an_oversize_body_before_any_work(editor_env: _EditorEnv) -> None:
    state, hidden = await editor_env.open_editor()
    calls_before = len(editor_env.github_calls)
    oversize = oauth_github._MAX_CONFIRM_BODY_BYTES + 1

    declared = await editor_env.browser.post(
        "/oauth/github/confirm",
        content=urlencode({**hidden, "padding": "x" * oversize}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert declared.status_code == 413
    assert "That selection is too large to save." in declared.text
    assert "Go back, choose fewer repos" in declared.text
    assert "Ask the requester" not in declared.text
    await _no_github_calls_after(editor_env, calls_before)


@pytest.mark.asyncio
async def test_confirm_refuses_a_declared_oversize_body_without_reading_it(
    editor_env: _EditorEnv,
) -> None:
    await editor_env.open_editor()
    read_chunks = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal read_chunks
        read_chunks += 1
        yield b"x"

    response = await editor_env.browser.post(
        "/oauth/github/confirm",
        content=body(),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(oauth_github._MAX_CONFIRM_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert read_chunks == 0


@pytest.mark.asyncio
async def test_confirm_refuses_too_many_fields(editor_env: _EditorEnv) -> None:
    state, hidden = await editor_env.open_editor()
    calls_before = len(editor_env.github_calls)
    # Small in bytes, so only the field-count limit can stop it.
    body = urlencode(hidden) + "&a=1" * (oauth_github._MAX_CONFIRM_FIELDS + 1)
    assert len(body) < oauth_github._MAX_CONFIRM_BODY_BYTES

    response = await editor_env.browser.post(
        "/oauth/github/confirm",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    await _no_github_calls_after(editor_env, calls_before)


def test_noscript_large_owner_list_uses_one_access_field() -> None:
    installations = [
        oauth_github._Installation(
            id=owner,
            owner_id=owner,
            owner_login=f"org-{owner}",
            repository_selection="selected",
            repos=tuple(
                oauth_github._Repo(
                    owner * 10_000 + index,
                    owner,
                    owner,
                    f"org-{owner}/repo-{index}",
                    "admin",
                )
                for index in range(2000)
            ),
        )
        for owner in range(1, 4)
    ]
    page = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="Editor",
        workspace="Test",
        platform="discord",
        grants=[],
        installations=installations,
        snapshot="snapshot",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.test/setup",
        github_user_id=1,
    ).body.decode()
    fallback = page.split("<noscript><section", 1)[1].split("</noscript>", 1)[0]
    assert fallback.count('name="repo"') == 6000
    footer = page.split('<div class="gh-editor-footer"', 1)[1].split("</form>", 1)[0]
    assert footer.startswith('><div class="gh-editor-footer-inner">')
    assert (
        '<button id="save-repos" class="gh-primary" type="submit">Save changes</button>' in footer
    )
    assert re.search(r'name="access_\d+"', fallback) is None
    assert page.count('name="access" value="write"') == 1
    # Every checkbox can be checked without parse_qs reaching max_num_fields.
    assert 6000 + len(_hidden_pairs(page)) + fallback.count('name="remove"') + 1 < (
        oauth_github._MAX_CONFIRM_FIELDS
    )


@pytest.mark.asyncio
async def test_editor_rejects_unknown_and_forged_scoped_fields(editor_env: _EditorEnv) -> None:
    _, hidden = await editor_env.open_editor()
    for extra in ({"forged": "write"}, {"access_103": "write"}):
        response = await editor_env.browser.post(
            "/oauth/github/confirm",
            data={**hidden, "working": "keep", "repo": "103", **extra},
        )
        assert response.status_code == 400
        assert "Selection could not be verified." in response.text
    assert await editor_env.saved_repo_ids() == []


@pytest.mark.asyncio
async def test_confirm_stops_reading_a_chunked_body_past_the_cap(editor_env: _EditorEnv) -> None:
    state, hidden = await editor_env.open_editor()
    calls_before = len(editor_env.github_calls)
    chunk = b"x" * 16_384
    total_chunks = 64  # 1 MiB, four times the cap
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(total_chunks):
            sent += 1
            yield chunk

    request = editor_env.browser.build_request(
        "POST",
        "/oauth/github/confirm",
        content=body(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert "content-length" not in request.headers

    response = await editor_env.browser.send(request)

    assert response.status_code == 413
    assert sent * len(chunk) <= oauth_github._MAX_CONFIRM_BODY_BYTES + 2 * len(chunk)
    assert sent < total_chunks
    await _no_github_calls_after(editor_env, calls_before)


@pytest.mark.asyncio
async def test_confirm_does_not_trust_a_short_content_length(editor_env: _EditorEnv) -> None:
    state, hidden = await editor_env.open_editor()
    calls_before = len(editor_env.github_calls)
    payload = b"x=" + b"y" * (oauth_github._MAX_CONFIRM_BODY_BYTES + 1)

    response = await editor_env.browser.post(
        "/oauth/github/confirm",
        content=payload,
        headers={"content-type": "application/x-www-form-urlencoded", "content-length": "10"},
    )

    assert response.status_code == 413
    await _no_github_calls_after(editor_env, calls_before)


@pytest.mark.asyncio
async def test_failed_save_retry_form_keeps_state_and_the_ticked_repos(
    editor_env: _EditorEnv,
) -> None:
    state, hidden = await editor_env.open_editor()
    editor_env.details_down = True

    failed = await editor_env.browser.post(
        "/oauth/github/confirm",
        data={
            **hidden,
            "access": "write",
            "working": "101",
            "repo": ["101", "102"],
        },
    )

    assert failed.status_code == 502
    assert "Couldn't reach GitHub" in html.unescape(failed.text)
    form = re.search(r'<form class="gh-actions" method="post" action="([^"]+)">', failed.text)
    assert form is not None
    # The POST has no state in its URL; the form carries it, and a link would
    # have been a state-less GET that lands on the expired page.
    assert form.group(1) == "https://mcp.test/oauth/github/confirm"
    assert "state=" not in form.group(1)
    carried = _hidden_pairs(failed.text)
    assert ("state", state) in carried
    assert [value for name, value in carried if name == "repo"] == ["101", "102"]
    assert dict(carried)["access"] == "write"
    assert dict(carried)["working"] == "101"
    assert dict(carried)["receipt"] == hidden["receipt"]

    # Pressing Try again is the browser posting exactly those fields.
    editor_env.details_down = False
    retried = await _submit(editor_env.browser, form.group(1), carried)
    assert retried.status_code == 200
    assert "Editor can read and change these 2 repos." in retried.text
    assert await editor_env.saved_repo_ids() == [101, 102]


@pytest.mark.asyncio
async def test_failed_get_keeps_a_try_again_link_with_state(editor_env: _EditorEnv) -> None:
    state, _hidden = await editor_env.open_editor()
    editor_env.listing_down = True

    failed = await editor_env.browser.get("/oauth/github/confirm", params={"state": state})

    assert failed.status_code == 200
    assert f'href="https://mcp.test/oauth/github/confirm?state={state}"' in failed.text
    assert "Could not load repos." in failed.text


@pytest.mark.asyncio
async def test_listing_failure_on_save_returns_a_page_that_can_retry(
    editor_env: _EditorEnv,
) -> None:
    """An owner disappearing after render requires a fresh page before any save."""
    state, hidden = await editor_env.open_editor()
    editor_env.listing_down = True

    failed = await editor_env.browser.post(
        "/oauth/github/confirm", data={**hidden, "access": "read", "working": "keep", "repo": "103"}
    )

    assert failed.status_code == 409
    assert await editor_env.saved_repo_ids() == []
    editor_env.listing_down = False
    refreshed = await editor_env.browser.get("/oauth/github/confirm", params={"state": state})
    retried = await editor_env.browser.post(
        "/oauth/github/confirm",
        data={**_hidden_fields(refreshed.text), "access": "read", "working": "keep", "repo": "103"},
    )
    assert retried.status_code == 200
    assert await editor_env.saved_repo_ids() == [103]


@pytest.mark.asyncio
async def test_slow_github_hits_the_total_deadline_with_selections_kept(
    editor_env: _EditorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, hidden = await editor_env.open_editor()
    monkeypatch.setattr(oauth_github, "_INSTALLATIONS_DEADLINE_SECONDS", 0.05)
    editor_env.listing_delay = 30.0  # would hold the request open without the deadline

    posted = await editor_env.browser.post(
        "/oauth/github/confirm",
        data={**hidden, "access": "read", "working": "keep", "repo": ["101", "102"]},
    )
    got = await editor_env.browser.get("/oauth/github/confirm", params={"state": state})

    assert posted.status_code == 502
    assert [value for name, value in _hidden_pairs(posted.text) if name == "repo"] == [
        "101",
        "102",
    ]
    assert got.status_code == 502
    assert f"confirm?state={state}" in got.text
    assert await editor_env.saved_repo_ids() == []


@pytest.mark.asyncio
async def test_admin_check_outage_on_save_keeps_the_selection(
    editor_env: _EditorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, hidden = await editor_env.open_editor()
    monkeypatch.setattr(
        oauth_github,
        "_attest_native_admin",
        AsyncMock(side_effect=oauth_github.AdminAttestationUnavailable("down")),
    )

    failed = await editor_env.browser.post(
        "/oauth/github/confirm", data={**hidden, "access": "read", "working": "keep", "repo": "103"}
    )

    assert failed.status_code == 503
    assert ("repo", "103") in _hidden_pairs(failed.text)
    assert await editor_env.saved_repo_ids() == []


def test_error_page_retry_form_escapes_and_drops_unknown_fields() -> None:
    page = oauth_github._error(
        "Couldn't reach GitHub",
        502,
        "https://mcp.test/oauth/github/confirm",
        retry_fields={
            "state": ['a"b'],
            "repo": ["1", "2"],
            "access_7": ["write"],
            "access_x": ["write"],
            "cookie": ["secret"],
        },
    ).body.decode()
    assert 'name="state" value="a&quot;b"' in page
    assert page.count('name="repo"') == 2
    assert 'name="access_7"' in page
    assert 'name="access_x"' not in page and 'name="cookie"' not in page
