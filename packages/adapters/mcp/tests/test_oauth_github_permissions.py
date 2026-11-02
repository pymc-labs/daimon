"""The Connect GitHub editor offers repos by the signed-in person's GitHub permission."""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
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
    TeamsSettings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminAttestationUnavailable
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners, github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant, set_role
from daimon.core.stores.domain import AccountIdentityRow, Role
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import (
    make_account,
    make_github_app_installation,
    make_platform_principal,
    make_tenant,
    mark_principal_foreign,
    set_github_installation_snapshot,
)
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

WRITE_INSTALLATION = {
    "metadata": "read",
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
}
READ_INSTALLATION = {
    "metadata": "read",
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
}
#: GitHub's `permissions` object for each level, as the repository listing reports it.
LEVELS: dict[str, dict[str, bool]] = {
    "read": {"pull": True},
    "triage": {"pull": True, "triage": True},
    "write": {"pull": True, "triage": True, "push": True},
    "maintain": {"pull": True, "triage": True, "push": True, "maintain": True},
    "admin": {"pull": True, "triage": True, "push": True, "maintain": True, "admin": True},
    "none": {},
}


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


@dataclass(frozen=True)
class _Org:
    installation_id: int
    owner_id: int
    login: str
    installation_permissions: Mapping[str, str]
    repos: list[tuple[int, str]]  # (repo id, level)


@dataclass
class _Editor:
    browser: httpx.AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID
    platform: Literal["discord", "slack"]

    async def page(
        self, *, requester_platform_user_id: str = "editor", status: int = 200
    ) -> tuple[dict[str, str], str]:
        async with self.sessionmaker.begin() as session:
            invitation = await github_connect.mint_invitation(
                session,
                tenant_id=self.tenant_id,
                requester_account_id=self.account_id,
                requester_label="Admin",
                requester_platform_user_id=requester_platform_user_id,
                agent_id=self.agent_id,
                agent_name="Editor",
                agent_ma_id="ag_editor",
                agent_manager_verified=True,
                operator_issued=False,
                origin_platform=self.platform,
            )
        start = await self.browser.get(f"/oauth/github/connect/{invitation}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        await self.browser.get("/oauth/github/callback", params={"state": state, "code": "code"})
        rendered = await self.browser.get("/oauth/github/confirm", params={"state": state})
        assert rendered.status_code == status
        hidden = {
            name: html.unescape(value)
            for name, value in re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)">', rendered.text
            )
        }
        return hidden, rendered.text

    async def save(self, hidden: dict[str, str], **choices: object) -> httpx.Response:
        return await self.browser.post(
            "/oauth/github/confirm",
            data={**hidden, "access": "read", "working": "keep", **choices},
        )

    async def grants(self) -> dict[int, str]:
        async with self.sessionmaker() as session:
            rows = await github_access.list_agent_repos(
                session, tenant_id=self.tenant_id, agent_id=self.agent_id
            )
        return {row.repo_id: row.ceiling_access for row in rows}


@asynccontextmanager
async def _editor(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    orgs: list[_Org],
    *,
    platform: Literal["discord", "slack"] = "discord",
    requester: Literal["admin", "owner", "co_owner", "member"] = "admin",
) -> AsyncIterator[_Editor]:
    monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=True))
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_editor")
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(
            session, id=tenant_id, workspace_id="perm-workspace", platform=platform
        )
        account = await make_account(session, tenant=tenant, id=account_id)
        if requester == "admin":
            await set_role(session, account_id, Role.ADMIN)
        await make_platform_principal(
            session, platform=platform, external_id="editor", tenant=tenant, account=account
        )
        if requester == "owner":
            await agent_owners.set_owner(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                owner_account_id=account_id,
                source="admin_assigned",
            )
        if requester == "co_owner":
            await agent_owners.add_co_owner(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=account_id,
                added_by_account_id=None,
            )

    def paged(request: httpx.Request, rows: list[dict[str, object]], key: str) -> httpx.Response:
        per_page = int(request.url.params["per_page"])
        page = int(request.url.params["page"])
        return httpx.Response(200, json={key: rows[(page - 1) * per_page : page * per_page]})

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-token"})
        if path == "/applications/client/token":
            return httpx.Response(204)
        if path == "/user":
            return httpx.Response(200, json={"id": 17})
        if path == "/user/installations":
            return paged(
                request,
                [
                    {
                        "id": org.installation_id,
                        "account": {"id": org.owner_id, "login": org.login, "type": "Organization"},
                        "repository_selection": "all",
                    }
                    for org in orgs
                ],
                "installations",
            )
        for org in orgs:
            if path == f"/user/installations/{org.installation_id}/repositories":
                return paged(
                    request,
                    [
                        {
                            "id": repo_id,
                            "owner": {"id": org.owner_id},
                            "full_name": f"{org.login}/repo-{repo_id}",
                            "permissions": LEVELS[level],
                        }
                        for repo_id, level in org.repos
                    ],
                    "repositories",
                )
            if path == f"/app/installations/{org.installation_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": org.installation_id,
                        "account": {"id": org.owner_id, "login": org.login, "type": "Organization"},
                        "repository_selection": "all",
                        "suspended_at": None,
                        "permissions": dict(org.installation_permissions),
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
        yield _Editor(browser, sessionmaker, tenant_id, agent_id, account_id, platform)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_agent_save_requires_live_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    platform: Literal["discord", "slack"],
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], platform=platform) as editor:
        hidden, _ = await editor.page()
        live = AsyncMock(side_effect=[False, AdminAttestationUnavailable("lookup failed"), True])
        monkeypatch.setattr(oauth_github, "_attest_native_admin", live)
        denied = await editor.save(hidden, repo="101")
        assert denied.status_code == 403
        assert "You can no longer manage this agent." in denied.text
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            assert (await github_connect.invitation_status(session, hidden["invitation"]))[
                0
            ] == "active"
        retry = await editor.save(hidden, repo="101")
        assert retry.status_code == 503
        assert "Try again in a minute." in retry.text
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            assert (await github_connect.invitation_status(session, hidden["invitation"]))[
                0
            ] == "active"
        saved = await editor.save(hidden, repo="101")
        assert saved.status_code == 200
        assert await editor.grants() == {101: "write"}
        assert live.await_count == 3


@pytest.mark.asyncio
async def test_agent_save_rejects_mismatched_requester_platform_user_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        hidden, _ = await editor.page(requester_platform_user_id="someone-else")
        live = AsyncMock(return_value=True)
        monkeypatch.setattr(oauth_github, "_attest_native_admin", live)
        denied = await editor.save(hidden, repo="101")
        assert denied.status_code == 403
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            assert (await github_connect.invitation_status(session, hidden["invitation"]))[
                0
            ] == "active"
        live.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "deleted", "expected"),
    [
        ((True, False, False), False, True),
        ((False, True, False), False, True),
        ((False, False, True), False, True),
        ((False, False, False), False, False),
        ((True, False, False), True, False),
    ],
)
async def test_slack_uses_live_workspace_role(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[bool, bool, bool],
    deleted: bool,
    expected: bool,
) -> None:
    live_attestation = oauth_github._attest_native_admin
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], platform="slack") as editor:
        key = Fernet.generate_key().decode()
        fernet = build_multifernet((key,))
        async with committing_sessionmaker.begin() as session:
            await upsert_slack_bot_token(
                session,
                team_id="perm-workspace",
                encrypted_token=encrypt_token(fernet, "xoxb-test"),
            )
            requester = await get_account_with_tenant(session, account_id=editor.account_id)
        assert requester is not None

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "slack.com"
            assert request.url.params["user"] == "editor"
            assert request.headers["Authorization"] == "Bearer xoxb-test"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user": {
                        "id": "editor",
                        "deleted": deleted,
                        "is_admin": flags[0],
                        "is_owner": flags[1],
                        "is_primary_owner": flags[2],
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert (
                await live_attestation(
                    requester=requester,
                    settings=_settings(key),
                    sessionmaker=committing_sessionmaker,
                    fernet=fernet,
                    http_client=client,
                )
                is expected
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        {"id": "editor", "is_admin": True, "is_owner": False},
        {"id": "editor", "is_admin": "true", "is_owner": False, "is_primary_owner": False},
        {"id": "other", "is_admin": True, "is_owner": False, "is_primary_owner": False},
    ],
)
async def test_slack_rejects_unbound_or_malformed_admin_facts(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    user: dict[str, object],
) -> None:
    live_attestation = oauth_github._attest_native_admin
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], platform="slack") as editor:
        key = Fernet.generate_key().decode()
        fernet = build_multifernet((key,))
        async with committing_sessionmaker.begin() as session:
            await upsert_slack_bot_token(
                session,
                team_id="perm-workspace",
                encrypted_token=encrypt_token(fernet, "xoxb-test"),
            )
            requester = await get_account_with_tenant(session, account_id=editor.account_id)
        assert requester is not None

        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "user": user})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(AdminAttestationUnavailable):
                await live_attestation(
                    requester=requester,
                    settings=_settings(key),
                    sessionmaker=committing_sessionmaker,
                    fernet=fernet,
                    http_client=client,
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("token_state", ["missing", "invalid"])
async def test_slack_requires_valid_workspace_bot_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    token_state: str,
) -> None:
    live_attestation = oauth_github._attest_native_admin
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], platform="slack") as editor:
        key = Fernet.generate_key().decode()
        fernet = build_multifernet((key,))
        async with committing_sessionmaker.begin() as session:
            if token_state == "invalid":
                await upsert_slack_bot_token(
                    session, team_id="perm-workspace", encrypted_token=b"invalid"
                )
            requester = await get_account_with_tenant(session, account_id=editor.account_id)
        assert requester is not None
        with pytest.raises(AdminAttestationUnavailable):
            await live_attestation(
                requester=requester,
                settings=_settings(key),
                sessionmaker=committing_sessionmaker,
                fernet=fernet,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "timeout"])
async def test_slack_lookup_failure_is_unavailable(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    live_attestation = oauth_github._attest_native_admin
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], platform="slack") as editor:
        key = Fernet.generate_key().decode()
        fernet = build_multifernet((key,))
        async with committing_sessionmaker.begin() as session:
            await upsert_slack_bot_token(
                session,
                team_id="perm-workspace",
                encrypted_token=encrypt_token(fernet, "xoxb-test"),
            )
            requester = await get_account_with_tenant(session, account_id=editor.account_id)
        assert requester is not None

        def respond(request: httpx.Request) -> httpx.Response:
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(AdminAttestationUnavailable):
                await live_attestation(
                    requester=requester,
                    settings=_settings(key),
                    sessionmaker=committing_sessionmaker,
                    fernet=fernet,
                    http_client=client,
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "expected"), [(True, True), (False, False)])
@pytest.mark.parametrize(
    "tenant_external_id",
    ["00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000A"],
)
async def test_teams_uses_live_admin_team_owners(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    owner: bool,
    expected: bool,
    tenant_external_id: str,
) -> None:
    key = Fernet.generate_key().decode()
    settings = _settings(key).model_copy(
        update={
            "teams": TeamsSettings(
                client_id="app-id",
                client_secret=SecretStr("app-secret"),
                tenant_id="00000000-0000-0000-0000-00000000000a",
                admin_team_id="00000000-0000-0000-0000-000000000002",
            )
        }
    )
    user_id = "00000000-0000-0000-0000-000000000003"
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="teams", workspace_id=tenant_external_id)
        principal = await make_platform_principal(
            session, tenant=tenant, platform="teams", external_id=user_id
        )
        requester = await get_account_with_tenant(session, account_id=principal.account_id)
    assert requester is not None

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == "Bearer graph-token"
        return httpx.Response(
            200,
            json={
                "value": [{"userId": user_id, "roles": ["owner"] if owner else [], "tenantId": "t"}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (
            await oauth_github._attest_native_admin(
                requester=requester,
                settings=settings,
                sessionmaker=committing_sessionmaker,
                fernet=build_multifernet((key,)),
                http_client=client,
            )
            is expected
        )


@pytest.mark.asyncio
async def test_teams_rejects_different_tenant_without_graph_lookup(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    settings = _settings(key).model_copy(
        update={
            "teams": TeamsSettings(
                client_id="app-id",
                client_secret=SecretStr("app-secret"),
                tenant_id="00000000-0000-0000-0000-000000000001",
                admin_team_id="00000000-0000-0000-0000-000000000002",
            )
        }
    )
    requester = AccountIdentityRow(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform="teams",
        external_id="00000000-0000-0000-0000-000000000004",
        platform_user_id="00000000-0000-0000-0000-000000000003",
    )

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected Graph request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        assert not await oauth_github._attest_native_admin(
            requester=requester,
            settings=settings,
            sessionmaker=committing_sessionmaker,
            fernet=build_multifernet((key,)),
            http_client=client,
        )


def _choices(page: str) -> dict[int, dict[str, str]]:
    """The picker dialog's repo choices, keyed by repo id, with their data attributes."""
    found = re.findall(r'<div class="gh-picker-choice" ([^>]*)>', page)
    return {
        int(attrs["data-repo-id"]): attrs
        for attrs in (dict(re.findall(r'(data-[\w-]+)="([^"]*)"', raw)) for raw in found)
    }


@pytest.mark.asyncio
async def test_picker_offers_every_permission_level_and_counts_hidden(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    levels = ["read", "triage", "write", "maintain", "admin", "none"]
    org = _Org(
        77, 55, "alpha", WRITE_INSTALLATION, [(i + 1, level) for i, level in enumerate(levels)]
    )
    other = _Org(78, 56, "beta", WRITE_INSTALLATION, [(201, "read"), (202, "none"), (203, "none")])
    async with _editor(committing_sessionmaker, monkeypatch, [org, other]) as editor:
        _hidden, page = await editor.page()
    choices = _choices(page)
    assert set(choices) == {1, 2, 3, 4, 5, 201}  # `none` repos are not offered
    assert [choices[i]["data-max-access"] for i in (1, 2, 3, 4, 5)] == ["write"] * 5
    assert choices[201]["data-installation"] == "78"
    assert "hidden because your GitHub account" not in page
    assert "Not listed?" in page and "Open GitHub setup" in page
    # The owner sheet only offers repos under that owner.
    assert "Everything in alpha</button>" not in page
    assert "Everything in beta</button>" not in page
    assert 'id="select-shown"' in page
    assert 'id="clear-selection"' in page
    assert 'name="picker_access"' not in page


@pytest.mark.asyncio
async def test_no_hidden_note_when_every_repo_is_usable(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(1, "read"), (2, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        _hidden, page = await editor.page()
    assert "hidden-repos" not in page and "hidden because" not in page


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["read", "triage", "write", "maintain", "admin"])
@pytest.mark.parametrize("installation_writes", [True, False])
async def test_app_write_grant_requires_installation_write_but_not_person_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    installation_writes: bool,
) -> None:
    permissions = WRITE_INSTALLATION if installation_writes else READ_INSTALLATION
    org = _Org(77, 55, "alpha", permissions, [(1, level)])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        hidden, _page = await editor.page()
        saved = await editor.save(hidden, repo="1", access="write")
        if not installation_writes:
            assert saved.status_code == 403
            assert await editor.grants() == {}
            return
        assert saved.status_code == 200
        assert await editor.grants() == {1: "write"}
        async with editor.sessionmaker() as session:
            [row] = await github_access.list_authorized_repos(
                session, tenant_id=editor.tenant_id, agent_id=editor.agent_id
            )
        assert row.max_access == "write"
        assert row.authorized_permission == level


@pytest.mark.asyncio
async def test_bulk_write_repos_in_one_installation(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = ["write", "maintain", "admin"]
    big = _Org(77, 55, "pymc-labs", WRITE_INSTALLATION, [(i, cycle[i % 3]) for i in range(1, 301)])
    readonly = _Org(
        78, 56, "readonly-co", READ_INSTALLATION, [(1000 + i, cycle[i % 3]) for i in range(78)]
    )
    org_levels = {repo_id: level for repo_id, level in big.repos}
    async with _editor(committing_sessionmaker, monkeypatch, [big, readonly]) as editor:
        hidden, page = await editor.page()
        # Every GitHub page was fetched: more than the 100 a page holds, and 378 in all.
        assert len(_choices(page)) == 378
        ids = sorted(org_levels)
        saved = await editor.save(hidden, repo=[str(repo_id) for repo_id in ids], access="write")
        assert saved.status_code == 200
        grants = await editor.grants()
    assert len(grants) == 300
    assert set(grants.values()) == {"write"}


@pytest.mark.asyncio
async def test_removing_or_adding_a_repo_with_no_permission_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(1, "write"), (2, "none")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        hidden, _page = await editor.page()
        assert (await editor.save(hidden, repo="2")).status_code == 403
        assert (await editor.save(hidden, repo="1")).status_code == 200


def test_repo_confirmation_uses_app_write_for_read_level_people() -> None:
    def repo(level: github_connect.GitHubPermission) -> oauth_github._Repo:
        return oauth_github._Repo(1, 2, 3, "o/r", level)

    def confirm(
        level: github_connect.GitHubPermission, *, needs_write: bool, installation: bool = True
    ) -> str:
        return oauth_github._repo_confirmation(
            repo(level),
            requested="read",
            needs_write=needs_write,
            installation_writes={3: installation},
        ).max_access

    for level in ("admin", "write", "triage", "read"):
        assert confirm(level, needs_write=True) == "write"
    with pytest.raises(ValueError, match="GitHub App needs write access"):
        confirm("admin", needs_write=True, installation=False)


@pytest.mark.asyncio
async def test_read_level_readd_after_admin_write_grant_stays_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader re-adding a repo receives the App's write grant."""
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(1, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        hidden, _page = await editor.page()
        assert (await editor.save(hidden, repo="1", access="write")).status_code == 200
        assert await editor.grants() == {1: "write"}

        hidden, _page = await editor.page()
        assert (await editor.save(hidden, remove="1")).status_code == 200
        assert await editor.grants() == {}

        org.repos[0] = (1, "read")
        hidden, _page = await editor.page()
        assert (await editor.save(hidden, repo="1", access="write")).status_code == 200
        assert await editor.grants() == {1: "write"}
        async with editor.sessionmaker() as session:
            [row] = await github_access.list_authorized_repos(
                session, tenant_id=editor.tenant_id, agent_id=editor.agent_id
            )
        assert (row.max_access, row.authorized_permission) == ("write", "read")


async def _remove_ownership(editor: _Editor) -> None:
    async with editor.sessionmaker.begin() as session:
        await agent_owners.remove_co_owner(
            session,
            tenant_id=editor.tenant_id,
            agent_id=editor.agent_id,
            account_id=editor.account_id,
        )
        await agent_owners.clear_owner(
            session, tenant_id=editor.tenant_id, agent_id=editor.agent_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["owner", "co_owner"])
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_an_owner_saves_repos_for_their_agent_without_being_a_platform_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    platform: Literal["discord", "slack"],
    requester: Literal["owner", "co_owner"],
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "read")])
    async with _editor(
        committing_sessionmaker, monkeypatch, [org], platform=platform, requester=requester
    ) as editor:
        hidden, page = await editor.page()
        assert "People also need their own" not in page
        live = AsyncMock(return_value=False)
        monkeypatch.setattr(oauth_github, "_attest_native_admin", live)
        saved = await editor.save(hidden, repo="101")
        assert saved.status_code == 200
        # Any repo GitHub shows the signer connects, and the grant is write by default.
        assert await editor.grants() == {101: "write"}
        live.assert_not_awaited()
        async with editor.sessionmaker() as session:
            account = await get_account_with_tenant(session, account_id=editor.account_id)
            repos = await github_access.list_agent_repos(
                session, tenant_id=editor.tenant_id, agent_id=editor.agent_id
            )
        assert account is not None and account.role == Role.USER, "an owner is never promoted"
        assert [repo.scope for repo in repos] == ["agent"], "an owner's repos are agent-scoped"


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["admin", "owner"])
async def test_owner_save_new_repo_with_stale_installation_coverage(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    requester: Literal["admin", "owner"],
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], requester=requester) as editor:
        # The last reconciliation saw no repos, but GitHub now lists this repo
        # to the signer and reports a healthy, write-capable installation.
        async with editor.sessionmaker.begin() as session:
            await make_github_app_installation(
                session,
                installation_id=org.installation_id,
                account_login=org.login,
                repo_full_names=[],
                github_repo_full_names=[],
            )

        hidden, page = await editor.page()
        assert "alpha/repo-101" in page
        response = await editor.save(hidden, repo="101", working="101")
        assert response.status_code == 200
        assert await editor.grants() == {101: "write"}
        async with editor.sessionmaker() as session:
            repos = await github_access.list_agent_repos(
                session, tenant_id=editor.tenant_id, agent_id=editor.agent_id
            )
            status, _ = await github_connect.invitation_status(session, hidden["invitation"])
        assert len(repos) == 1 and repos[0].is_working_repo
        assert status == "used"


@pytest.mark.asyncio
async def test_newer_installation_refresh_during_save_requires_reload(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        initial_refresh = datetime.now(UTC) - timedelta(minutes=1)
        async with editor.sessionmaker.begin() as session:
            await make_github_app_installation(
                session,
                installation_id=org.installation_id,
                account_login=org.login,
                repo_full_names=["alpha/repo-101"],
                github_repo_full_names=["alpha/repo-101"],
                github_refreshed_at=initial_refresh,
            )
        hidden, page = await editor.page()
        assert "alpha/repo-101" in page

        original_installations = oauth_github._installations  # pyright: ignore[reportPrivateUsage]
        refresh_completed = False

        async def refresh_after_repo_fetch(*args: Any, **kwargs: Any) -> object:
            nonlocal refresh_completed
            installations = await original_installations(*args, **kwargs)
            if kwargs.get("before_repo_fetch") is not None:
                async with editor.sessionmaker.begin() as session:
                    await set_github_installation_snapshot(
                        session,
                        installation_id=org.installation_id,
                        repo_full_names=[],
                        refreshed_at=initial_refresh + timedelta(seconds=1),
                    )
                refresh_completed = True
            return installations

        monkeypatch.setattr(oauth_github, "_installations", refresh_after_repo_fetch)
        response = await editor.save(hidden, repo="101")
        assert refresh_completed
        assert response.status_code == 409
        assert "The agent's repos changed. Reload and try again." in html.unescape(response.text)
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            installation = await github_app_installations.get(
                session, installation_id=org.installation_id
            )
            status, _ = await github_connect.invitation_status(session, hidden["invitation"])
        assert installation is not None
        assert installation.github_repo_full_names == ()
        assert status == "active"


@pytest.mark.asyncio
async def test_unexpected_grant_value_error_is_private_and_not_retried(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org]) as editor:
        hidden, _ = await editor.page()
        secret_error = "private repo/token sentinel"
        monkeypatch.setattr(
            github_access, "stage_grant", AsyncMock(side_effect=ValueError(secret_error))
        )
        response = await editor.save(hidden, repo="101")
        assert response.status_code == 500
        assert html.unescape(response.text).count("<h1>Your changes weren't saved.</h1>") == 1
        assert "Return to your chat and ask an admin for help." in response.text
        assert "Try again" not in response.text
        assert "new setup link" not in response.text
        assert secret_error not in response.text
        assert secret_error not in caplog.text
        failures = [
            record
            for record in caplog.records
            if record.getMessage() == "github_connect.save_failed"
        ]
        assert len(failures) == 1
        assert vars(failures[0])["stage"] == "stage_grant"
        assert vars(failures[0])["error_type"] == "ValueError"
        assert secret_error not in str(failures)
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            status, _ = await github_connect.invitation_status(session, hidden["invitation"])
        assert status == "active"


@pytest.mark.asyncio
async def test_an_owner_removed_between_mint_and_save_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], requester="owner") as editor:
        hidden, _ = await editor.page()
        await _remove_ownership(editor)
        monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=False))
        denied = await editor.save(hidden, repo="101")
        assert denied.status_code == 403
        assert "You can no longer manage this agent." in denied.text
        assert await editor.grants() == {}
        async with editor.sessionmaker() as session:
            assert (await github_connect.invitation_status(session, hidden["invitation"]))[
                0
            ] == "active"


@pytest.mark.asyncio
async def test_an_owner_removed_during_the_save_is_refused_inside_the_transaction(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner is read again in the write transaction, never a stale stored admin role."""
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], requester="owner") as editor:
        async with editor.sessionmaker.begin() as session:
            await set_role(session, editor.account_id, Role.ADMIN)  # stale stored role
        hidden, _ = await editor.page()
        monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=False))
        reads = AsyncMock(side_effect=[True, False])  # owner at the first check, gone by the save
        monkeypatch.setattr(oauth_github, "owns_agent", reads)
        denied = await editor.save(hidden, repo="101")
        assert denied.status_code == 403
        assert reads.await_count == 2
        assert await editor.grants() == {}


@pytest.mark.asyncio
async def test_a_member_cannot_open_or_save_the_agent_editor(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], requester="member") as editor:
        monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=False))
        # The page itself is refused, and so is a save posted without it.
        hidden, refused = await editor.page(status=403)
        assert "You can no longer manage this agent." in refused
        assert await editor.grants() == {}


@pytest.mark.asyncio
async def test_a_person_from_another_organisation_cannot_save_as_an_owner(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with _editor(committing_sessionmaker, monkeypatch, [org], requester="owner") as editor:
        hidden, _ = await editor.page()
        async with editor.sessionmaker.begin() as session:
            await mark_principal_foreign(session, account_id=editor.account_id)
        monkeypatch.setattr(oauth_github, "_attest_native_admin", AsyncMock(return_value=False))
        denied = await editor.save(hidden, repo="101")
        assert denied.status_code == 403
        assert await editor.grants() == {}


@pytest.mark.asyncio
async def test_an_owner_cannot_hold_a_server_wide_connect_link(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _Org(77, 55, "alpha", WRITE_INSTALLATION, [(101, "admin")])
    async with (
        _editor(committing_sessionmaker, monkeypatch, [org], requester="owner") as editor,
        editor.sessionmaker.begin() as session,
    ):
        with pytest.raises(ValueError, match="tenant admin"):
            await github_connect.mint_invitation(
                session,
                tenant_id=editor.tenant_id,
                requester_account_id=editor.account_id,
                requester_platform_user_id="editor",
                agent_manager_verified=True,
            )
