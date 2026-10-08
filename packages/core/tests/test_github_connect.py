"""Connection invitation and requester access properties."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import APIStatusError
from cryptography.fernet import Fernet
from daimon.core import github_app_session
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentGitHubGrant,
    CliPrincipal,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    GitHubUserLink,
    PlatformPrincipal,
    Tenant,
    TenantGitHubRepo,
    ThreadSession,
)
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    AppSessionAccess,
    archive_app_vault,
    close_headless_app_session,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.github_requester_access import (
    PermissionCache,
    effective_access,
    linked_permissions,
)
from daimon.core.stores import (
    github_app_installations,
    github_connect,
    github_issued_tokens,
    github_links,
)
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def test_effective_access_properties() -> None:
    levels = ("none", "read", "write")
    for ceiling in ("read", "write"):
        for asker in levels:
            result = effective_access({1: ceiling}, {1: asker}).get(1, "none")
            assert levels.index(result) == min(levels.index(ceiling), levels.index(asker))
        assert effective_access({1: ceiling}, None) == {1: ceiling}
    assert effective_access({1: "write"}, {}) == {}
    assert effective_access({}, None) == {}


async def test_human_without_link_or_repo_permission_gets_typed_reason(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, account_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db_session_factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=101,
                owner_id=1,
                installation_id=77,
                repo_full_name="example/repo",
                max_access="read",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=77,
            account_id=1,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=101,
                baseline_access="read",
                ceiling_access="read",
                staged=False,
                version=1,
            )
        )

    config = GithubAppSettings(
        app_id="123",
        private_key=SecretStr("unused"),
        client_id="client",
        client_secret=SecretStr("secret"),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as client:
        missing = await github_app_session.prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            is_external=False,
            provisional_session_id="unlinked",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert missing.tokens == () and missing.reason == "needs_link"
        assert (
            await github_app_session.effective_repo_state(
                db_session_factory,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=account_id,
                is_external=False,
                config=config,
                fernet=fernet,
            )
        ) == ((), {}, "needs_link")

        async with db_session_factory.begin() as session:
            session.add(
                GitHubUserLink(
                    github_user_id=501,
                    login="person",
                    encrypted_access_token=b"encrypted",
                    access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.flush()
            session.add(
                AccountGitHubLink(
                    account_id=account_id,
                    github_user_id=501,
                    platform="discord",
                    platform_user_id="person",
                    verified_via="discord_oauth",
                )
            )
        monkeypatch.setattr(github_app_session, "linked_permissions", AsyncMock(return_value={}))
        denied = await github_app_session.prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            is_external=False,
            provisional_session_id="no-permission",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert denied.tokens == () and denied.reason == "no_github_access"
        assert (
            await github_app_session.effective_repo_state(
                db_session_factory,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=account_id,
                is_external=False,
                config=config,
                fernet=fernet,
            )
        ) == ((), {}, "no_github_access")


def test_permission_cache_evicts_least_recently_used() -> None:
    cache = PermissionCache(max_entries=2)
    cache.put(1, 1, 1, {1: "read"})
    cache.put(1, 2, 1, {2: "read"})
    assert cache.get(1, 1, 1) == {1: "read"}
    cache.put(1, 3, 1, {3: "write"})
    assert cache.get(1, 2, 1) is None
    assert cache.get(1, 1, 1) == {1: "read"}


@pytest.mark.asyncio
async def test_invitation_is_admin_only_and_single_use(db_session: AsyncSession) -> None:
    tenant_id, admin_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(Account(id=member_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=member_id
        )
    external_id = uuid.uuid4()
    db_session.add(Account(id=external_id, tenant_id=tenant_id, role="admin", is_external=True))
    await db_session.flush()
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=external_id
        )
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_label="Alex",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.workspace_label == "discord workspace workspace"
    assert invitation.requester_label == "Alex"
    assert invitation.expires_at > datetime.now(UTC) + timedelta(days=6)
    admin = await db_session.get(Account, admin_id)
    assert admin is not None
    admin.is_external = True
    await db_session.flush()
    assert await github_connect.get_invitation(db_session, github_connect.digest(token)) is None
    admin.is_external = False
    await db_session.flush()
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.get_flow(db_session, state="state", cookie="wrong") is None
    assert await github_connect.get_flow(db_session, state="state", cookie="cookie") is not None
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="other-browser",
        cookie="other-cookie",
        encrypted_verifier=b"encrypted",
    )
    saved = await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/repo",
                max_access="read",
            )
        ],
    )
    assert saved
    assert await db_session.get(GitHubConnectFlow, github_connect.digest("other-browser")) is None
    assert await github_connect.get_invitation(db_session, github_connect.digest(token)) is None
    assert not await github_connect.confirm(
        db_session, state="state", cookie="cookie", github_user_id=17, repos=[]
    )
    expired_token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    expired = await db_session.get(GitHubConnectInvitation, github_connect.digest(expired_token))
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert (
        await github_connect.get_invitation(db_session, github_connect.digest(expired_token))
        is None
    )
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(expired_token),
        state="expired-flow",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    expired_flow = await db_session.get(GitHubConnectFlow, github_connect.digest("expired-flow"))
    assert expired_flow is not None
    expired_flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert await github_connect.get_flow(db_session, state="expired-flow", cookie="cookie") is None
    assert await github_connect.delete_expired_flows(db_session, now=datetime.now(UTC)) == 1
    assert await db_session.get(GitHubConnectFlow, github_connect.digest("expired-flow")) is None


@pytest.mark.asyncio
async def test_connect_link_requester_resolves_platform_admin_not_cli_operator(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    admin_id, cli_id, member_id, external_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
            Account(id=cli_id, tenant_id=tenant_id, role="user"),
            Account(id=member_id, tenant_id=tenant_id, role="user"),
            Account(id=external_id, tenant_id=tenant_id, role="admin", is_external=True),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            CliPrincipal(tenant_id=tenant_id, os_user="operator", account_id=cli_id),
            PlatformPrincipal(
                tenant_id=tenant_id, platform="discord", external_id="123", account_id=admin_id
            ),
            PlatformPrincipal(
                tenant_id=tenant_id, platform="discord", external_id="456", account_id=member_id
            ),
            PlatformPrincipal(
                tenant_id=tenant_id, platform="discord", external_id="789", account_id=external_id
            ),
            PlatformPrincipal(
                tenant_id=tenant_id,
                platform="slack",
                external_id="wrong-platform",
                account_id=admin_id,
            ),
        ]
    )
    await db_session.flush()

    assert (
        await github_connect.cli_account_id(db_session, tenant_id=tenant_id, os_user="operator")
        == cli_id
    )
    requester_id = await github_connect.admin_account_for_platform_user(
        db_session, tenant_id=tenant_id, external_id="123"
    )
    assert requester_id == admin_id
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=requester_id,
        requester_label="123",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.requester_account_id == admin_id

    for platform_user_id in ("456", "789", "wrong-platform", "missing"):
        with pytest.raises(ValueError, match="workspace admin"):
            await github_connect.admin_account_for_platform_user(
                db_session, tenant_id=tenant_id, external_id=platform_user_id
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_status", "expected_access"),
    [("active", "write"), ("revoked", "read"), ("suspended", "read")],
)
async def test_reconfirmation_respects_inactive_repo_choice(
    db_session: AsyncSession, previous_status: str, expected_access: str
) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=55,
            installation_id=77,
            repo_full_name="example/repo",
            max_access="write",
            authorized_by_github_user_id=17,
            status=previous_status,
            version=1,
        )
    )
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/repo",
                max_access="read",
            )
        ],
    )
    repo = await db_session.get(TenantGitHubRepo, (tenant_id, 101))
    assert repo is not None
    assert repo.status == "active" and repo.max_access == expected_access
    assert repo.version == 2


@pytest.mark.asyncio
async def test_refresh_serializes_for_one_github_user(
    db_engine: AsyncEngine, db_nullpool_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    user_id = 98765
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=user_id,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes

        async def probe_row_lock() -> None:
            async with probe_sessionmaker.begin() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                assert await github_links.get_user_for_update(session, github_user_id=user_id)

        await asyncio.wait_for(probe_row_lock(), timeout=15)
        if request.url.path == "/login/oauth/access_token":
            refreshes += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "new",
                    "refresh_token": "refresh-new",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 123, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache(ttl_seconds=0)
        left, right = await asyncio.gather(
            *(
                linked_permissions(
                    sessionmaker,
                    client,
                    user_id=user_id,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
                for _ in range(2)
            )
        )
    assert left == right == {123: "read"}
    assert refreshes == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=user_id)
    assert row is not None and row.token_generation == 2 and row.status == "active"


@pytest.mark.asyncio
async def test_bad_refresh_token_does_not_break_a_rotated_link(
    db_engine: AsyncEngine, db_nullpool_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=681,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/login/oauth/access_token":
            refreshes += 1
            async with probe_sessionmaker.begin() as session:
                assert await github_links.rotate_user_tokens(
                    session,
                    github_user_id=681,
                    expected_generation=1,
                    encrypted_access_token=encrypt_token(fernet, "new"),
                    encrypted_refresh_token=encrypt_token(fernet, "refresh-new"),
                    access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            return httpx.Response(200, json={"error": "bad_refresh_token"})
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 101, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        permissions = await linked_permissions(
            sessionmaker,
            client,
            user_id=681,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert permissions == {101: "read"}
    assert refreshes == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=681)
    assert row is not None and row.status == "active"
    assert row.token_generation == 2 and row.link_generation == 1


@pytest.mark.asyncio
async def test_expired_refresh_does_not_break_a_rotated_link(
    db_engine: AsyncEngine,
    db_nullpool_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=682,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )

    original_bump = github_links.bump_link_generation
    raced = False

    async def bump_with_race(session: AsyncSession, **kwargs: object):
        nonlocal raced
        if not raced:
            raced = True
            async with probe_sessionmaker.begin() as probe:
                assert await github_links.rotate_user_tokens(
                    probe,
                    github_user_id=682,
                    expected_generation=1,
                    encrypted_access_token=encrypt_token(fernet, "new"),
                    encrypted_refresh_token=encrypt_token(fernet, "refresh-new"),
                    access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
        return await original_bump(session, **kwargs)

    monkeypatch.setattr("daimon.core.github_requester_access.bump_link_generation", bump_with_race)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 101, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        permissions = await linked_permissions(
            sessionmaker,
            client,
            user_id=682,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert permissions == {101: "read"}
    assert raced
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=682)
    assert row is not None and row.status == "active"
    assert row.token_generation == 2 and row.link_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "broken"),
    [
        (200, {"error": "bad_refresh_token"}, True),
        (503, {"message": "unavailable"}, False),
        (429, {"message": "rate limited"}, False),
        (200, {"error": "incorrect_client_credentials"}, False),
    ],
)
async def test_refresh_only_breaks_invalid_grant(
    db_engine: AsyncEngine,
    db_clean: None,
    status: int,
    body: dict[str, str],
    broken: bool,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=678,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = linked_permissions(
            sessionmaker,
            client,
            user_id=678,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
        if broken:
            assert await call == {}
        elif body.get("error"):
            with pytest.raises(RuntimeError, match="incorrect_client_credentials"):
                await call
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await call
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=678)
    assert row is not None
    assert (row.status == "broken") is broken
    assert row.link_generation == (2 if broken else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_repo_permission_denial_is_cached_as_empty(
    db_engine: AsyncEngine, db_clean: None, status: int
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=679,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": "not available"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache()
        for _ in range(2):
            assert (
                await linked_permissions(
                    sessionmaker,
                    client,
                    user_id=679,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
                == {}
            )
    assert calls == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=679)
    assert row is not None and row.status == "active" and row.link_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"x-ratelimit-remaining": "0"}, "rate limited"),
        ({"retry-after": "60"}, "rate limited"),
        ({}, "You have exceeded a secondary rate limit."),
    ],
)
async def test_repo_permission_rate_limit_is_not_cached(
    db_engine: AsyncEngine, db_clean: None, headers: dict[str, str], message: str
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=680,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, headers=headers, json={"message": message})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache()
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await linked_permissions(
                    sessionmaker,
                    client,
                    user_id=680,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
    assert calls == 2


@pytest.mark.asyncio
async def test_user_token_401_invalidates_link(db_engine: AsyncEngine, db_clean: None) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=1234,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unauthorized)) as client:
        result = await linked_permissions(
            sessionmaker,
            client,
            user_id=1234,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert result == {}
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1234)
    assert row is not None and row.status == "broken" and row.link_generation == 2


@pytest.mark.asyncio
async def test_unlink_bumps_generation_and_deletes_last_token(db_session: AsyncSession) -> None:
    tenant_id, account_a, account_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=account_a, tenant_id=tenant_id, role="user"))
    db_session.add(Account(id=account_b, tenant_id=tenant_id, role="user"))
    db_session.add(
        GitHubUserLink(
            github_user_id=1234,
            login="alex",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    for account_id in (account_a, account_b):
        db_session.add(
            AccountGitHubLink(
                account_id=account_id,
                github_user_id=1234,
                platform="discord",
                platform_user_id=str(account_id),
                verified_via="discord_oauth",
            )
        )
    await db_session.flush()
    assert await github_links.unlink_account(db_session, account_id=account_a) == 1234
    user = await github_links.get_user(db_session, github_user_id=1234)
    assert user is not None and user.link_generation == 2
    assert await github_links.unlink_account(db_session, account_id=account_b) == 1234
    assert await github_links.get_user(db_session, github_user_id=1234) is None


@pytest.mark.asyncio
async def test_issued_tokens_stale_after_bump_unlink_and_relink(db_session: AsyncSession) -> None:
    tenant_id, account_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
    await github_app_installations.upsert(
        db_session, installation_id=909, account_login="example", repo_full_names=["example/repo"]
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=1,
            installation_id=909,
            repo_full_name="example/repo",
            max_access="read",
            authorized_by_github_user_id=501,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            staged=False,
            is_working_repo=True,
            version=1,
        )
    )
    db_session.add(
        GitHubUserLink(
            github_user_id=501,
            login="first",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    db_session.add(
        AccountGitHubLink(
            account_id=account_id,
            github_user_id=501,
            platform="discord",
            platform_user_id="person",
            verified_via="discord_oauth",
        )
    )
    await db_session.flush()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    async def issued(generation: int) -> uuid.UUID:
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=f"session-{generation}",
            installation_id=909,
            repo_ids=[101],
            permissions={"contents": "read"},
            grant_versions={"grant:101": 1, "authorization:101": 1},
            requester_account_id=account_id,
            github_user_id=501,
            link_generation=generation,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token="issued", fernet=fernet
        )
        return row.token_id

    first = await issued(1)
    assert await github_issued_tokens.select_stale_tokens(db_session) == []
    await github_links.bump_link_generation(db_session, github_user_id=501)
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first
    }
    second = await issued(2)
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first
    }
    assert await github_links.unlink_account(db_session, account_id=account_id) == 501
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first,
        second,
    }

    db_session.add(
        GitHubUserLink(
            github_user_id=777,
            login="second",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    db_session.add(
        AccountGitHubLink(
            account_id=account_id,
            github_user_id=777,
            platform="discord",
            platform_user_id="person",
            verified_via="discord_oauth",
        )
    )
    await db_session.flush()
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first,
        second,
    }


@pytest.mark.asyncio
async def test_headless_app_session_is_closed_only_after_runner_finishes(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="headless-test"))
    await db_session.flush()
    token = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="headless-session",
        installation_id=909,
        repo_ids=[101],
        permissions={"contents": "read"},
        grant_versions={"grant:101": 1, "authorization:101": 1},
        expires_at=datetime.now(UTC) + timedelta(minutes=55),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await github_issued_tokens.store_token(
        db_session, token_id=token.token_id, token="headless-token", fernet=fernet
    )
    await github_issued_tokens.mark_delivered(db_session, token_id=token.token_id)
    started = datetime.now(UTC)
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="headless-session", except_ids=frozenset(), now=started
    )
    assert (
        await github_issued_tokens.select_due_superseded_tokens(
            db_session, now=started + timedelta(seconds=119)
        )
        == []
    )
    assert [
        row.token_id
        for row in await github_issued_tokens.select_due_superseded_tokens(
            db_session, now=started + timedelta(seconds=120)
        )
    ] == [token.token_id]
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id="headless-session", tenant_id=tenant_id, vault_id="session-vault"
    )
    await db_session.flush()

    now = datetime.now(UTC) + timedelta(minutes=2)
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=now) == []

    assert (
        await github_issued_tokens.finish_headless_app_session(
            db_session, session_id="headless-session"
        )
        == "session-vault"
    )
    closed = await github_issued_tokens.list_closed_app_sessions(db_session, now=now)
    assert [(row.session_id, row.vault_id) for row in closed] == [
        ("headless-session", "session-vault")
    ]
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="headless-session"
    )
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=now) == []


@pytest.mark.asyncio
async def test_headless_app_cleanup_archives_vault_without_grants(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="headless-zero"))
    await db_session.flush()
    await github_issued_tokens.register_headless_app_session(
        db_session,
        session_id="headless-zero-grants",
        tenant_id=tenant_id,
        vault_id="session-vault",
    )
    await db_session.commit()
    archive = AsyncMock()
    anthropic = SimpleNamespace(beta=SimpleNamespace(vaults=SimpleNamespace(archive=archive)))
    await close_headless_app_session(
        anthropic, db_session_factory, session_id="headless-zero-grants", fernet=None
    )
    archive.assert_awaited_once_with("session-vault")
    async with db_session_factory() as session:
        assert (
            await github_issued_tokens.list_closed_app_sessions(session, now=datetime.now(UTC))
            == []
        )


@pytest.mark.asyncio
async def test_mapped_app_vault_closes_without_issued_tokens(db_session: AsyncSession) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="mapped-zero"))
    await db_session.flush()
    await github_issued_tokens.register_app_session_vault(
        db_session,
        session_id="mapped-zero-grants",
        tenant_id=tenant_id,
        vault_id="mapped-vault",
    )
    mapping = ThreadSession(
        tenant_id=tenant_id,
        platform="discord",
        thread_id="mapped-zero-thread",
        ma_session_id="mapped-zero-grants",
        status="live",
    )
    db_session.add(mapping)
    await db_session.flush()
    assert (
        await github_issued_tokens.touch_unmapped_app_session(
            db_session, session_id="mapped-zero-grants"
        )
        is None
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC)) == []
    )
    mapping.status = "dead"
    await db_session.flush()
    closed = await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC))
    assert [(row.session_id, row.vault_id) for row in closed] == [
        ("mapped-zero-grants", "mapped-vault")
    ]
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="mapped-zero-grants"
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC)) == []
    )


@pytest.mark.asyncio
async def test_unmapped_mcp_vault_survives_followups_until_the_turn_ceiling(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="mcp-unmapped"))
    await db_session.flush()
    await github_issued_tokens.register_app_session_vault(
        db_session,
        session_id="mcp-session",
        tenant_id=tenant_id,
        vault_id="mcp-vault",
        is_unmapped=True,
    )
    started = datetime.now(UTC)
    await db_session.execute(
        text(
            "UPDATE github_app_session_vaults SET last_started_at = :old "
            "WHERE session_id = 'mcp-session'"
        ),
        {"old": started - timedelta(minutes=40)},
    )
    assert await github_issued_tokens.list_closed_app_sessions(
        db_session, now=started + timedelta(minutes=7)
    )
    await github_issued_tokens.touch_unmapped_app_session(db_session, session_id="mcp-session")
    assert (
        await github_issued_tokens.list_closed_app_sessions(
            db_session, now=started + timedelta(minutes=7)
        )
        == []
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(
            db_session, now=started + timedelta(minutes=45)
        )
        == []
    )
    closed = await github_issued_tokens.list_closed_app_sessions(
        db_session, now=started + timedelta(minutes=47)
    )
    assert [(row.session_id, row.vault_id) for row in closed] == [("mcp-session", "mcp-vault")]
    assert (
        await github_issued_tokens.finish_headless_app_session(db_session, session_id="mcp-session")
        == "mcp-vault"
    )
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=started) == closed
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="mcp-session"
    )
    assert (
        await github_issued_tokens.touch_unmapped_app_session(db_session, session_id="mcp-session")
        is False
    )


@pytest.mark.asyncio
async def test_app_vault_archive_accepts_already_archived() -> None:
    response = httpx.Response(
        404,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/vaults/archived/archive"),
    )
    archive = AsyncMock(
        side_effect=APIStatusError("Vault already archived", response=response, body=None)
    )
    anthropic = SimpleNamespace(beta=SimpleNamespace(vaults=SimpleNamespace(archive=archive)))
    await archive_app_vault(anthropic, vault_id="archived")
    archive.assert_awaited_once_with("archived")


@pytest.mark.asyncio
async def test_app_rotation_keeps_old_session_when_first_resource_update_fails(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    access = AppSessionAccess(
        resources=(
            {
                "type": "github_repository",
                "url": "https://github.com/owner/repo",
                "authorization_token": "new-token",
            },
        ),
        tokens=(),
        working_token=None,
    )
    monkeypatch.setattr(github_app_session, "prepare_app_access", AsyncMock(return_value=access))
    revoke = AsyncMock()
    monkeypatch.setattr(github_app_session, "revoke_app_access", revoke)
    update = AsyncMock(side_effect=RuntimeError("resource update failed"))
    archive = AsyncMock()
    anthropic = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(resources=SimpleNamespace(update=update), archive=archive)
        )
    )
    with pytest.raises(RuntimeError, match="resource update failed"):
        await github_app_session.rotate_live_app_tokens(
            anthropic,
            db_session_factory,
            session_id="existing-session",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            account_id=None,
            is_external=True,
            vault_id="existing-vault",
            resource_ids={"https://github.com/owner/repo": "resource-1"},
            config=GithubAppSettings(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        )
    archive.assert_not_awaited()
    revoke.assert_awaited_once()
