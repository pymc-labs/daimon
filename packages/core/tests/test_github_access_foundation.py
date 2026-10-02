"""Repository-scoped token minting and durable inventory checks."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core._models import AgentGitHubGrant, Tenant, TenantGitHubRepo
from daimon.core.config import GithubAppSettings, GithubSettings, load_settings
from daimon.core.github_app_auth import group_repository_access, mint_installation_token
from daimon.core.github_credentials import build_multifernet
from daimon.core.security_audit import GITHUB_TOKEN_MINT
from daimon.core.stores import github_access, github_app_installations, github_issued_tokens
from daimon.core.stores.security_audit import append_github_token_event
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest.mark.asyncio
async def test_mint_rejects_empty_ids_before_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty repository set reached GitHub")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await mint_installation_token(client, jwt="jwt", installation_id=9, repository_ids=[])


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
            permissions={"contents": "read"},
        )
    assert token == "issued"
    assert json.loads(requests[0].content) == {
        "repository_ids": [10, 11],
        "permissions": {"contents": "read"},
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
    authorization = await db_session.get(TenantGitHubRepo, (tenant_id, 101))
    assert authorization is not None
    authorization.version = 2
    await db_session.flush()
    assert [
        item.token_id for item in await github_issued_tokens.select_stale_tokens(db_session)
    ] == [row.token_id]
    event = await append_github_token_event(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        kind=GITHUB_TOKEN_MINT,
        outcome="allowed",
        reason="issued",
    )
    assert event is not None and event.operation == "github_token_mint"
