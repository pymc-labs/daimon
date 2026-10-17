"""Owner grant resolution and name-scoped token inventory on PostgreSQL."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core._models import GitHubAppInstallation, GitHubIssuedToken
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import has_declared_app_access, prepare_app_access
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.stores.github_issued_tokens import select_stale_tokens
from daimon.core.stores.github_owner_grants import (
    clear_exclusion,
    exclude_repo,
    remove_owner_grants,
    resolve_owner_repositories,
)
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _owner_setup(
    factory: async_sessionmaker[AsyncSession], count: int = 3
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    names = [f"example/repo-{index}" for index in range(count)]
    async with factory.begin() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, platform, external_id) VALUES (:id, 'discord', :external_id)"
            ),
            {"id": tenant_id, "external_id": str(tenant_id)},
        )
        session.add(
            GitHubAppInstallation(
                installation_id=771,
                account_login="example",
                app="github_app",
                account_id=1,
                account_type="Organization",
                repository_selection="all",
                repo_full_names=[names[0]],
                github_repo_full_names=names[:1],
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos (tenant_id, repo_id, owner_id, installation_id, repo_full_name, max_access, authorized_by_github_user_id, status) VALUES (:tenant, 101, 1, 771, :name, 'read', 2, 'active')"
            ),
            {"tenant": tenant_id, "name": names[0]},
        )
        await session.execute(
            text(
                "INSERT INTO agent_github_owner_grants (tenant_id, agent_id, installation_id, owner_login, access) VALUES (:tenant, :agent, 771, 'example', 'read')"
            ),
            {"tenant": tenant_id, "agent": agent_id},
        )
    return tenant_id, agent_id


@pytest.mark.asyncio
async def test_future_snapshot_exclusion_and_removal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, agent_id = await _owner_setup(db_session_factory)
    assert await has_declared_app_access(db_session_factory, tenant_id=tenant_id, agent_id=agent_id)
    async with db_session_factory() as session:
        assert [
            row.full_name
            for row in await resolve_owner_repositories(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        ] == ["example/repo-0"]
    async with db_session_factory.begin() as session:
        installation = await session.get(GitHubAppInstallation, 771)
        assert installation is not None
        installation.github_repo_full_names = ["example/repo-0", "example/repo-1"]
    async with db_session_factory() as session:
        assert {
            row.full_name
            for row in await resolve_owner_repositories(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        } == {"example/repo-0", "example/repo-1"}

    async with db_session_factory.begin() as session:
        await exclude_repo(
            session, tenant_id=tenant_id, agent_id=agent_id, repo_full_name="example/repo-1"
        )
        assert await remove_owner_grants(
            session, tenant_id=tenant_id, agent_id=agent_id, owner_login="example"
        )
        await session.execute(
            text(
                "INSERT INTO agent_github_owner_grants (tenant_id, agent_id, installation_id, owner_login, access) VALUES (:tenant, :agent, 771, 'example', 'read')"
            ),
            {"tenant": tenant_id, "agent": agent_id},
        )
    async with db_session_factory() as session:
        assert [
            row.full_name
            for row in await resolve_owner_repositories(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        ] == ["example/repo-0"]
    async with db_session_factory.begin() as session:
        await clear_exclusion(
            session, tenant_id=tenant_id, agent_id=agent_id, repo_full_name="example/repo-1"
        )
    async with db_session_factory() as session:
        assert {
            row.full_name
            for row in await resolve_owner_repositories(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        } == {"example/repo-0", "example/repo-1"}


@pytest.mark.asyncio
async def test_mixed_case_exclusion_is_absent_from_minted_token(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, agent_id = await _owner_setup(db_session_factory)
    async with db_session_factory.begin() as session:
        installation = await session.get(GitHubAppInstallation, 771)
        assert installation is not None
        installation.github_repo_full_names = ["example/repo-0", "example/repo-1"]
        await exclude_repo(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_full_name="Example/Repo-0",
        )
    async with db_session_factory() as session:
        assert [
            repo.full_name
            for repo in await resolve_owner_repositories(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        ] == ["example/repo-1"]
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    minted_names: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        names = json.loads(request.content)["repositories"]
        minted_names.append(names)
        return httpx.Response(201, json={"token": "scoped-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            provisional_session_id="mixed-case-owner",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
        )
    assert minted_names == [["repo-1"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse_first", [False, True])
async def test_owner_names_chunk_and_refusal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    refuse_first: bool,
) -> None:
    tenant_id, agent_id = await _owner_setup(db_session_factory, count=503)
    async with db_session_factory.begin() as session:
        installation = await session.get(GitHubAppInstallation, 771)
        assert installation is not None
        installation.github_repo_full_names = [f"example/repo-{index}" for index in range(503)]
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    groups: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        assert "repository_ids" not in body
        names = body["repositories"]
        groups.append(names)
        if refuse_first and len(groups) == 1:
            return httpx.Response(403)
        return httpx.Response(201, json={"token": f"token-{len(groups)}"})

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            provisional_session_id="owner-session",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=fernet,
            cache=PermissionCache(),
        )
    assert sorted(map(len, groups)) == [3, 500]
    assert len(access.tokens) == (1 if refuse_first else 2)
    assert len(access.excluded_repo_urls) == (500 if refuse_first else 0)
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.session_id == "owner-session")
            )
        )
        assert sorted(row.status for row in rows) == (
            ["revoked", "stored"] if refuse_first else ["stored", "stored"]
        )
        assert sum(len(row.repo_names) for row in rows) == 503
    async with db_session_factory.begin() as session:
        await exclude_repo(
            session, tenant_id=tenant_id, agent_id=agent_id, repo_full_name="example/repo-0"
        )
    async with db_session_factory() as session:
        stale = await select_stale_tokens(session, include_live_version_bumps=True)
        if not refuse_first:
            assert len(stale) == 1


@pytest.mark.asyncio
async def test_removed_snapshot_repo_is_not_minted(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, agent_id = await _owner_setup(db_session_factory, count=2)
    async with db_session_factory.begin() as session:
        installation = await session.get(GitHubAppInstallation, 771)
        assert installation is not None
        installation.github_repo_full_names = ["example/repo-0", "example/repo-1"]
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    scopes: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        scopes.append(json.loads(request.content)["repositories"])
        return httpx.Response(201, json={"token": "synthetic"})

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            provisional_session_id="before-loss",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        async with db_session_factory.begin() as session:
            installation = await session.get(GitHubAppInstallation, 771)
            assert installation is not None
            installation.github_repo_full_names = ["example/repo-0"]
        await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            provisional_session_id="after-loss",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
    assert scopes == [["repo-0", "repo-1"], ["repo-0"]]
    async with db_session_factory() as session:
        assert len(await select_stale_tokens(session)) == 1
