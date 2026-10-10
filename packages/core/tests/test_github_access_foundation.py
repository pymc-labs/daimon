"""Repository-scoped token minting and durable inventory checks."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.core._models import (
    Account,
    AgentGitHubGrant,
    GitHubIssuedToken,
    GitHubNewRepoNotice,
    GitHubRemovalNotice,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings, GithubSettings, load_settings
from daimon.core.github_app_auth import group_repository_access, mint_installation_token
from daimon.core.github_app_session import add_app_credentials, prepare_app_access
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_removal_delivery import poll_removal_notices_once
from daimon.core.github_requester_access import PermissionCache
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.security_audit import GITHUB_TOKEN_MINT
from daimon.core.stores import github_access, github_app_installations, github_issued_tokens
from daimon.core.stores.github_access_requests import get_request, request_access
from daimon.core.stores.github_new_repo_notices import queue_new_repos
from daimon.core.stores.github_removal_notices import (
    RemovalNotice,
    cancel_unconfirmed_removal,
    confirm_removal,
    queue_removal,
)
from daimon.core.stores.security_audit import append_github_token_event
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_new_app_settings_are_separate_from_legacy() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----"
    new = GithubAppSettings(
        private_key=SecretStr(base64.b64encode(pem.encode()).decode()), app_slug="new-app"
    )
    legacy = GithubSettings(app_id="old-app")
    assert new.private_key is not None and new.private_key.get_secret_value() == pem
    assert legacy.app_id == "old-app" and legacy.app_private_key is None
    with pytest.raises(ValueError):
        GithubAppSettings(app_slug="bad/path")


def test_new_app_env_does_not_change_legacy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_GITHUB__APP_ID", "legacy")
    monkeypatch.setenv("DAIMON_GITHUB_APP__APP_ID", "new")
    monkeypatch.setenv("DAIMON_GITHUB_APP__CLIENT_ID", "client")
    settings = load_settings(_env_file=None)
    assert settings.github.app_id == "legacy"
    assert settings.github_app.app_id == "new"
    assert settings.github_app.client_id == "client"


def test_group_access_splits_profiles_and_batches() -> None:
    grants: list[tuple[int, int, Literal["read", "write"]]] = [
        (9, repo_id, "read") for repo_id in range(1, 502)
    ]
    grants.append((9, 800, "write"))
    grants.append((10, 900, "read"))
    groups = group_repository_access(grants)
    assert [(installation, profile, len(ids)) for installation, profile, ids in groups] == [
        (9, "read", 500),
        (9, "read", 1),
        (9, "write", 1),
        (10, "read", 1),
    ]
    assert groups[0][2][0] == 1 and groups[1][2] == (501,)


def test_group_access_uses_write_for_duplicate_repo() -> None:
    groups = group_repository_access(
        [(9, 101, "read"), (9, 101, "write"), (9, 101, "read"), (9, 102, "read")]
    )
    assert groups == [(9, "write", (101,)), (9, "read", (102,))]


@pytest.mark.asyncio
async def test_staged_grants_activate_atomically_and_validate_ceiling(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session,
        installation_id=77,
        account_login="example",
        repo_full_names=["example/repo"],
    )
    db_session.add(
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
    await db_session.flush()
    with pytest.raises(ValueError, match="exceeds repository authorization"):
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=101,
            baseline_access="read",
            ceiling_access="write",
            granted_by_account_id=None,
        )
    staged = await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="none",
        ceiling_access="read",
        granted_by_account_id=None,
        is_working_repo=True,
    )
    assert staged.staged
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )
    await github_access.activate_agent(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "app"
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert len(grants) == 1 and not grants[0].staged and grants[0].version == 2
    updated = await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=None,
        is_working_repo=True,
    )
    assert not updated.staged and updated.version == 3 and updated.baseline_access == "read"
    await github_access.deactivate_agent(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )


@pytest.mark.asyncio
async def test_copied_agent_identity_starts_without_source_grants(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    source_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_source")
    copied_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_copy")
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session,
        installation_id=77,
        account_login="example",
        repo_full_names=["example/repo"],
    )
    db_session.add(
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
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=source_id,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=None,
    )
    await github_access.activate_agent(db_session, tenant_id=tenant_id, agent_id=source_id)
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=copied_id)
        == "legacy"
    )
    assert (
        await github_access.list_agent_grants(db_session, tenant_id=tenant_id, agent_id=copied_id)
        == []
    )


@pytest.mark.asyncio
async def test_new_installation_repo_is_queued_for_connected_workspace_admins(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=1,
            installation_id=77,
            repo_full_name="example/old",
            max_access="read",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    now = datetime.now(UTC)
    queued = await queue_new_repos(
        db_session,
        installation_id=77,
        old_names={"example/old"},
        new_names={"example/old", "example/new"},
        now=now,
    )
    repeated = await queue_new_repos(
        db_session,
        installation_id=77,
        old_names={"example/old"},
        new_names={"example/old", "example/new"},
        now=now,
    )
    notices = list(await db_session.scalars(select(GitHubNewRepoNotice)))
    assert queued == 1 and repeated == 0
    assert [(row.tenant_id, row.repo_full_name) for row in notices] == [(tenant_id, "example/new")]


@pytest.mark.asyncio
async def test_github_removal_waits_for_confirmation_then_notifies_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    asker_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=1,
            installation_id=77,
            repo_full_name="example/old",
            max_access="read",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    now = datetime.now(UTC)
    waiting = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=uuid.uuid4(),
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/old",
        requested_work="Continue the report",
        is_admin=False,
        now=now,
    )
    await queue_removal(db_session, installation_id=77, account_login="example", now=now)
    before = await get_request(db_session, tenant_id=tenant_id, request_id=waiting.id)
    assert before is not None and before.status == "open"
    notices = list(await db_session.scalars(select(GitHubRemovalNotice)))
    assert len(notices) == 1 and notices[0].confirmed_at is None
    delivered: list[str] = []

    async def deliver(notice: object) -> bool:
        assert isinstance(notice, RemovalNotice)
        delivered.append(notice.account_login)
        return True

    assert (
        await poll_removal_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 0
    )
    await confirm_removal(db_session, installation_id=77, now=now)
    after = await get_request(db_session, tenant_id=tenant_id, request_id=waiting.id)
    assert after is not None and after.status == "cancelled"
    assert (
        await poll_removal_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 1
    )
    assert (
        await poll_removal_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 0
    )
    assert delivered == ["example"]
    await cancel_unconfirmed_removal(db_session, installation_id=77)
    assert len(list(await db_session.scalars(select(GitHubRemovalNotice)))) == 1


@pytest.mark.asyncio
async def test_app_zero_grants_and_external_asker_mint_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))

    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected GitHub call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
        zero = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            provisional_session_id="pending",
            config=GithubAppSettings(),
            fernet=None,
            cache=PermissionCache(),
        )
        external = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=uuid.uuid4(),
            is_external=True,
            provisional_session_id="pending",
            config=GithubAppSettings(),
            fernet=None,
            cache=PermissionCache(),
        )
    assert zero.resources == external.resources == ()
    assert zero.tokens == external.tokens == ()
    assert zero.working_token is external.working_token is None

    async def empty_credentials(*, vault_id: str):
        if False:
            yield vault_id

    credentials = SimpleNamespace(list=empty_credentials, create=AsyncMock())
    fake_anthropic = cast(
        AsyncAnthropic,
        SimpleNamespace(beta=SimpleNamespace(vaults=SimpleNamespace(credentials=credentials))),
    )
    await add_app_credentials(fake_anthropic, vault_id="vault", access=zero)
    credentials.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_mint_revocation_race_revokes_late_github_token(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        await github_app_installations.upsert(
            session,
            installation_id=771,
            account_login="example",
            repo_full_names=["example/repo"],
        )
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=991,
                owner_id=1,
                installation_id=771,
                repo_full_name="example/repo",
                max_access="read",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=991,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
        )
        await github_access.activate_agent(session, tenant_id=tenant_id, agent_id=agent_id)

    async def mint_after_revocation(*args: object, **kwargs: object) -> str:
        async with db_session_factory.begin() as session:
            pending = await session.scalar(
                select(GitHubIssuedToken).where(
                    GitHubIssuedToken.tenant_id == tenant_id,
                    GitHubIssuedToken.agent_id == agent_id,
                    GitHubIssuedToken.status == "pending",
                )
            )
            assert pending is not None
            await github_issued_tokens.mark_revoked(session, token_id=pending.token_id)
        return "late-token"

    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(
        "daimon.core.github_app_session.mint_installation_token", mint_after_revocation
    )
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        deleted.append(request.headers["Authorization"])
        return httpx.Response(204)

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(github_issued_tokens.GitHubTokenRowClosedError):
            await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                is_external=False,
                provisional_session_id="pending-race",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )
    assert deleted == ["Bearer late-token"]
    async with db_session_factory() as session:
        row = await session.scalar(
            select(GitHubIssuedToken).where(GitHubIssuedToken.session_id == "pending-race")
        )
        assert row is not None and row.status == "revoked" and row.encrypted_token is None


@pytest.mark.asyncio
async def test_mint_rejects_empty_ids_before_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty repository set reached GitHub")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await mint_installation_token(
                client, jwt="jwt", installation_id=9, repository_ids=[], profile="read"
            )


@pytest.mark.asyncio
async def test_id_mint_requires_profile_before_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unprofiled ID mint reached GitHub")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await mint_installation_token(client, jwt="jwt", installation_id=9, repository_ids=[10])
        with pytest.raises(ValueError):
            await mint_installation_token(
                client,
                jwt="jwt",
                installation_id=9,
                repository_ids=[10],
                profile="read",
                permissions={"contents": "write"},
            )


@pytest.mark.asyncio
async def test_mint_uses_repository_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"token": "issued"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await mint_installation_token(
            client,
            jwt="jwt",
            installation_id=9,
            repository_ids=[10, 11],
            profile="read",
        )
    assert token == "issued"
    assert json.loads(requests[0].content) == {
        "repository_ids": [10, 11],
        "permissions": {
            "metadata": "read",
            "contents": "read",
            "issues": "read",
            "pull_requests": "read",
        },
    }


@pytest.mark.asyncio
async def test_inventory_sweeper_finds_changed_grant_version(db_session: AsyncSession) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
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
            authorized_by_github_user_id=2,
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
    await db_session.flush()
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )
    with pytest.raises(ValueError):
        await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="session",
            installation_id=909,
            repo_ids=[101],
            permissions={"contents": "read"},
            grant_versions={"grant:101": 1},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    row = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="session",
        installation_id=909,
        repo_ids=[101],
        permissions={"contents": "read"},
        grant_versions={"grant:101": 1, "authorization:101": 1},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    stored = await github_issued_tokens.store_token(
        db_session, token_id=row.token_id, token="secret", fernet=fernet
    )
    assert github_issued_tokens.decrypt_issued_token(stored, fernet=fernet) == "secret"
    assert await github_issued_tokens.select_stale_tokens(db_session) == []
    grant = await db_session.get(AgentGitHubGrant, (tenant_id, agent_id, 101))
    assert grant is not None
    grant.version = 2
    await db_session.flush()
    assert [
        item.token_id for item in await github_issued_tokens.select_stale_tokens(db_session)
    ] == [row.token_id]
    grant.version = 1
    authorization = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert authorization is not None
    authorization.version = 2
    await db_session.flush()
    assert [
        item.token_id for item in await github_issued_tokens.select_stale_tokens(db_session)
    ] == [row.token_id]
    origin_id = uuid.uuid4()
    event = await append_github_token_event(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        kind=GITHUB_TOKEN_MINT,
        outcome="allowed",
        reason="issued",
        token_id=row.token_id,
        session_id="session",
        installation_id=909,
        repo_ids=[101],
        permissions={"contents": "read"},
        expires_at=row.expires_at,
        grant_versions=row.grant_versions,
        turn_origin_id=origin_id,
    )
    assert event is not None and event.operation == "github_token_mint"
    assert event.github_token_id == row.token_id
    assert event.github_session_id == "session"
    assert event.github_installation_id == 909
    assert event.github_repo_ids == [101]
    assert event.github_permissions == {"contents": "read"}
    assert event.github_grant_versions == {"grant:101": 1, "authorization:101": 1}
    assert event.github_turn_origin_id == origin_id
    assert event.github_expires_at == row.expires_at

    await github_issued_tokens.record_revoke_attempt(db_session, token_id=row.token_id)
    revoked = await github_issued_tokens.mark_revoked(db_session, token_id=row.token_id)
    again = await github_issued_tokens.mark_revoked(db_session, token_id=row.token_id)
    assert revoked.revoked_at == again.revoked_at
    assert revoked.revoke_attempts == again.revoke_attempts == 1

    pending = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="pending-session",
        installation_id=909,
        repo_ids=[101],
        permissions={"contents": "read"},
        grant_versions={"grant:101": 1, "authorization:101": 2},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    closed = await github_issued_tokens.mark_revoked(db_session, token_id=pending.token_id)
    closed_again = await github_issued_tokens.mark_revoked(db_session, token_id=pending.token_id)
    assert closed.status == "revoked" and closed.encrypted_token is None
    assert closed_again.revoked_at == closed.revoked_at
    with pytest.raises(github_issued_tokens.GitHubTokenRowClosedError):
        await github_issued_tokens.store_token(
            db_session, token_id=pending.token_id, token="late-secret", fernet=fernet
        )
