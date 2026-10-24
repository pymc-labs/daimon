"""Repository-scoped token minting and durable inventory checks."""

from __future__ import annotations

import asyncio
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
    GitHubAppInstallation,
    GitHubIssuedToken,
    GitHubNewRepoNotice,
    GitHubRemovalNotice,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings, load_settings
from daimon.core.github_app_auth import (
    PermissionProfile,
    group_repository_access,
    mint_installation_token,
)
from daimon.core.github_app_session import (
    add_app_credentials,
    installation_credential_name,
    prepare_app_access,
    projected_vault_credential_count,
    require_vault_budget,
    token_scopes_for_groups,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_removal_delivery import poll_removal_notices_once
from daimon.core.github_requester_access import PermissionCache
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.security_audit import GITHUB_TOKEN_MINT
from daimon.core.session_snapshot import token_scope_identity
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def test_app_settings_decode_key_and_validate_slug() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----"
    new = GithubAppSettings(
        private_key=SecretStr(base64.b64encode(pem.encode()).decode()), app_slug="new-app"
    )
    assert new.private_key is not None and new.private_key.get_secret_value() == pem
    with pytest.raises(ValueError):
        GithubAppSettings(app_slug="bad/path")


def test_app_env_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_GITHUB_APP__APP_ID", "new")
    monkeypatch.setenv("DAIMON_GITHUB_APP__CLIENT_ID", "client")
    settings = load_settings(_env_file=None)
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


def test_multi_installation_chunk_names_scopes_and_budget() -> None:
    grants: list[tuple[int, int, Literal["read", "write"]]] = [
        (72, repo_id, "write") for repo_id in range(1, 502)
    ]
    grants.append((71, 900, "write"))
    groups = group_repository_access(grants)
    assert groups == [
        (72, "write", tuple(range(1, 501))),
        (72, "write", (501,)),
        (71, "write", (900,)),
    ]
    assert [
        installation_credential_name(installation, chunk)
        for installation, chunk in ((72, 0), (72, 1), (71, 0))
    ] == ["GH_TOKEN_I72_0", "GH_TOKEN_I72_1", "GH_TOKEN_I71_0"]
    assert token_scope_identity(token_scopes_for_groups(groups)) == (
        (71, (900,)),
        (72, tuple(range(1, 501))),
        (72, (501,)),
    )
    require_vault_budget(groups, other_credentials=10)
    with pytest.raises(ValueError, match="15-credential"):
        require_vault_budget(groups, other_credentials=11)
    assert projected_vault_credential_count([(71, "write", (900,))], other_credentials=2) == 4
    assert projected_vault_credential_count(groups, other_credentials=2) == 7
    assert projected_vault_credential_count(groups, other_credentials=2, include_alias=False) == 5
    require_vault_budget([(71, "write", (900,))], other_credentials=13)
    twelve_installations: list[tuple[int, PermissionProfile, tuple[int, ...]]] = [
        (number, "write", (number,)) for number in range(1, 13)
    ]
    assert (
        projected_vault_credential_count(
            twelve_installations, other_credentials=3, include_alias=False
        )
        == 15
    )
    require_vault_budget(twelve_installations, other_credentials=3, include_alias=False)
    with pytest.raises(ValueError, match="12 installations"):
        require_vault_budget([(number, "write", (number,)) for number in range(1, 14)])


@pytest.mark.asyncio
async def test_grants_are_live_immediately(
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
    assert staged.is_working_repo
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert len(grants) == 1 and grants[0].is_working_repo and grants[0].version == 1
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
    assert updated.version == 2 and updated.baseline_access == "read"
    installation = await db_session.get(GitHubAppInstallation, 77)
    assert installation is not None
    assert (
        len(
            await github_access.list_live_grant_repositories(
                db_session, tenant_id=tenant_id, agent_id=agent_id
            )
        )
        == 1
    )
    installation.github_repo_full_names = []
    await db_session.flush()
    assert (
        await github_access.list_live_grant_repositories(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
        == []
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
            max_access="write",
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
    assert (
        await github_access.list_agent_grants(db_session, tenant_id=tenant_id, agent_id=copied_id)
        == []
    )


@pytest.mark.asyncio
async def test_agent_can_grant_second_and_third_installations(db_session: AsyncSession) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    for installation_id, repo_id, name in (
        (71, 101, "first/one"),
        (72, 102, "second/two"),
        (73, 103, "third/three"),
    ):
        await github_app_installations.upsert(
            db_session,
            installation_id=installation_id,
            account_login=name.split("/", 1)[0],
            repo_full_names=[name],
        )
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=installation_id,
                installation_id=installation_id,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        granted_by_account_id=None,
        agent_name="ResearchBot",
    )
    for repo_id in (102, 103):
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=None,
            agent_name="ResearchBot",
        )
    assert {
        grant.repo_id
        for grant in await github_access.list_agent_grants(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
    } == {101, 102, 103}


@pytest.mark.asyncio
@pytest.mark.parametrize("suspend_existing", [False, True])
async def test_agent_refuses_thirteenth_installation_but_allows_more_repos_in_existing_one(
    db_session: AsyncSession,
    suspend_existing: bool,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    for installation_id in range(1, 14):
        name = f"owner{installation_id}/repo{installation_id}"
        await github_app_installations.upsert(
            db_session,
            installation_id=installation_id,
            account_login=f"owner{installation_id}",
            repo_full_names=[name],
        )
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=installation_id,
                owner_id=installation_id,
                installation_id=installation_id,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    for installation_id in range(1, 13):
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=installation_id,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=None,
            agent_name="ResearchBot",
        )
    if suspend_existing:
        installation = await db_session.get(GitHubAppInstallation, 1)
        assert installation is not None
        installation.suspended_at = datetime.now(UTC)
        await db_session.flush()
    with pytest.raises(
        github_access.InstallationLimitError,
        match=(
            r"ResearchBot can use repos from up to 12 GitHub accounts or orgs, "
            r"and this change would make it 13\. Nothing was saved\."
        ),
    ):
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=13,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=None,
            agent_name="ResearchBot",
        )
    assert (
        len(
            await github_access.list_agent_grants(
                db_session, tenant_id=tenant_id, agent_id=agent_id
            )
        )
        == 12
    )
    if suspend_existing:
        installation = await db_session.get(GitHubAppInstallation, 1)
        assert installation is not None
        installation.suspended_at = None
        await db_session.flush()
    await github_app_installations.upsert(
        db_session,
        installation_id=1,
        account_login="owner1",
        repo_full_names=["owner1/repo1", "owner1/extra"],
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=100,
            owner_id=1,
            installation_id=1,
            repo_full_name="owner1/extra",
            max_access="write",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=100,
        baseline_access="write",
        ceiling_access="write",
        granted_by_account_id=None,
        agent_name="ResearchBot",
    )
    assert (
        len(
            await github_access.list_agent_grants(
                db_session, tenant_id=tenant_id, agent_id=agent_id
            )
        )
        == 13
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["grant", "suspended", "set_working"])
async def test_501st_repo_at_twelve_installations_exceeds_session_vault_budget(
    db_session: AsyncSession,
    scenario: str,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    for installation_id in range(1, 13):
        repo_ids = range(1, 502) if installation_id == 1 else (500 + installation_id,)
        names = [f"owner{installation_id}/repo{repo_id}" for repo_id in repo_ids]
        await github_app_installations.upsert(
            db_session,
            installation_id=installation_id,
            account_login=f"owner{installation_id}",
            repo_full_names=names,
        )
        for repo_id, name in zip(repo_ids, names, strict=True):
            db_session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
            if installation_id != 1 or repo_id <= 500:
                db_session.add(
                    AgentGitHubGrant(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo_id,
                        baseline_access="write",
                        ceiling_access="write",
                        is_working_repo=(
                            scenario != "set_working" and installation_id == 1 and repo_id == 1
                        ),
                    )
                )
    await db_session.flush()
    if scenario == "suspended":
        installation = await db_session.get(GitHubAppInstallation, 2)
        assert installation is not None
        installation.suspended_at = datetime.now(UTC)
        await db_session.flush()
    assert (
        github_access.projected_session_credential_count(
            12, multi_installation=True, include_alias=True
        )
        == 15
    )
    if scenario == "set_working":
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=501,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=None,
            agent_name="ResearchBot",
        )
        with pytest.raises(
            github_access.SessionVaultBudgetError,
            match="ResearchBot can't take more GitHub repos: its session would need more than 15 credentials. Remove a repo or an account first.",
        ):
            await github_access.set_working_repo(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_name="owner1/repo1",
                account_id=uuid.uuid4(),
                agent_name="ResearchBot",
            )
        assert not any(
            grant.is_working_repo
            for grant in await github_access.list_agent_grants(
                db_session, tenant_id=tenant_id, agent_id=agent_id
            )
        )
    else:
        with pytest.raises(
            github_access.SessionVaultBudgetError,
            match="ResearchBot can't take more GitHub repos: its session would need more than 15 credentials. Remove a repo or an account first.",
        ):
            await github_access.stage_grant(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=501,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
                agent_name="ResearchBot",
            )
    assert len(
        await github_access.list_agent_grants(db_session, tenant_id=tenant_id, agent_id=agent_id)
    ) == (512 if scenario == "set_working" else 511)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_first", "suspend_existing"), [(True, False), (False, False), (True, True)]
)
async def test_multi_installation_grant_refuses_read_only_access(
    db_session: AsyncSession, read_first: bool, suspend_existing: bool
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    for installation_id in (1, 2):
        name = f"owner{installation_id}/repo{installation_id}"
        await github_app_installations.upsert(
            db_session,
            installation_id=installation_id,
            account_login=f"owner{installation_id}",
            repo_full_names=[name],
        )
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=installation_id,
                owner_id=installation_id,
                installation_id=installation_id,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=1,
        baseline_access="read" if read_first else "write",
        ceiling_access="read" if read_first else "write",
        granted_by_account_id=None,
        agent_name="ResearchBot",
    )
    if suspend_existing:
        installation = await db_session.get(GitHubAppInstallation, 1)
        assert installation is not None
        installation.suspended_at = datetime.now(UTC)
        await db_session.flush()
    with pytest.raises(
        github_access.ReadOnlyMultiInstallationError,
        match="ResearchBot has read-only GitHub repos. Make them read-write or remove them before adding repos from another account or org.",
    ):
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=2,
            baseline_access="write" if read_first else "read",
            ceiling_access="write" if read_first else "read",
            granted_by_account_id=None,
            agent_name="ResearchBot",
        )
    assert [
        grant.repo_id
        for grant in await github_access.list_agent_grants(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
    ] == [1]


@pytest.mark.asyncio
async def test_parallel_first_grants_keep_both_installations(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id, repo_id, name in ((71, 101, "first/one"), (72, 102, "second/two")):
            await github_app_installations.upsert(
                session,
                installation_id=installation_id,
                account_login=name.split("/", 1)[0],
                repo_full_names=[name],
            )
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
    first_staged = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with factory.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=101,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
            )
            first_staged.set()
            await release_first.wait()

    async def second() -> None:
        await first_staged.wait()
        async with factory.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=102,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
            )

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    try:
        await asyncio.wait_for(first_staged.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not second_task.done()
    finally:
        release_first.set()
        await first_task
    await second_task
    async with factory() as session:
        grants = await github_access.list_agent_grants(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    assert {grant.repo_id for grant in grants} == {101, 102}


@pytest.mark.asyncio
async def test_concurrent_twelfth_installation_grants_admit_exactly_one(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id in range(1, 14):
            name = f"owner{installation_id}/repo{installation_id}"
            await github_app_installations.upsert(
                session,
                installation_id=installation_id,
                account_login=f"owner{installation_id}",
                repo_full_names=[name],
            )
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=installation_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
            if installation_id <= 11:
                session.add(
                    AgentGitHubGrant(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=installation_id,
                        baseline_access="write",
                        ceiling_access="write",
                        is_working_repo=installation_id == 1,
                    )
                )
    first_staged = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with factory.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=12,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
            )
            first_staged.set()
            await release_first.wait()

    async def second() -> None:
        await first_staged.wait()
        async with factory.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=13,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
            )

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    try:
        await asyncio.wait_for(first_staged.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not second_task.done(), "the second grant waits for the per-agent lock"
    finally:
        release_first.set()
        await first_task
    with pytest.raises(github_access.InstallationLimitError):
        await second_task
    async with factory() as session:
        grants = await github_access.list_agent_grants(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    assert len(grants) == 12
    assert {grant.repo_id for grant in grants} == set(range(1, 13))


@pytest.mark.asyncio
async def test_crashed_provisional_token_is_not_revoked_before_github_expiry(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    now = datetime.now(UTC)
    pending = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="pending:crash-after-vault-swap",
        installation_id=1,
        repo_ids=[11],
        permissions={"contents": "write"},
        grant_versions={"grant:11": 1, "authorization:11": 1},
        expires_at=now + timedelta(minutes=55),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await github_issued_tokens.store_token(
        db_session, token_id=pending.token_id, token="maybe-in-vault", fernet=fernet
    )
    assert (
        await github_issued_tokens.select_abandoned_pending_tokens(
            db_session, now=now + timedelta(minutes=3)
        )
        == []
    )
    assert [
        row.token_id
        for row in await github_issued_tokens.select_abandoned_pending_tokens(
            db_session, now=now + timedelta(minutes=62)
        )
    ] == [pending.token_id]


@pytest.mark.asyncio
async def test_sweep_rechecks_restored_token_but_revokes_removed_grant(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session, installation_id=71, account_login="acme", repo_full_names=["acme/repo"]
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=1,
            installation_id=71,
            repo_full_name="acme/repo",
            max_access="write",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        granted_by_account_id=None,
    )
    [grant] = await github_access.list_agent_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    now = datetime.now(UTC)
    pending = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="sweep-race",
        installation_id=71,
        repo_ids=[101],
        permissions={"contents": "write"},
        grant_versions={"grant:101": grant.version, "authorization:101": 1},
        expires_at=now + timedelta(minutes=55),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await github_issued_tokens.store_token(
        db_session, token_id=pending.token_id, token="still-live", fernet=fernet
    )
    await github_issued_tokens.mark_delivered(db_session, token_id=pending.token_id)
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="sweep-race", except_ids=frozenset(), now=now
    )
    assert (
        await github_issued_tokens.recheck_token_revoke_due(
            db_session, token_id=pending.token_id, now=now
        )
        is not None
    )
    await github_issued_tokens.restore_session_tokens(
        db_session, token_ids=frozenset({pending.token_id})
    )
    assert (
        await github_issued_tokens.recheck_token_revoke_due(
            db_session, token_id=pending.token_id, now=now
        )
        is None
    )
    await github_access.remove_grant(
        db_session, tenant_id=tenant_id, agent_id=agent_id, repo_id=101
    )
    checked = await github_issued_tokens.recheck_token_revoke_due(
        db_session, token_id=pending.token_id, now=now
    )
    assert checked is not None and checked[0].token_id == pending.token_id
    assert not checked[1]


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
async def test_app_zero_grants_mint_nothing(
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
            provisional_session_id="pending",
            config=GithubAppSettings(),
            fernet=None,
            cache=PermissionCache(),
        )
    assert zero.resources == (), "no grant, no resource"
    assert zero.tokens == (), "no grant, no token"
    assert zero.working_token is None, "no grant, no working token"

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
                max_access="write",
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

    monkeypatch.setattr(
        "daimon.core.github_app_session.build_app_jwt",
        lambda *a, **k: "jwt",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
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
async def test_inventory_sweeper_keeps_write_token_for_read_authorizer_until_version_changes(
    db_session: AsyncSession,
) -> None:
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
            baseline_access="write",
            ceiling_access="write",
            is_working_repo=True,
            version=1,
        )
    )
    await db_session.flush()
    with pytest.raises(ValueError):
        await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="session",
            installation_id=909,
            repo_ids=[101],
            permissions={"contents": "write"},
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
        permissions={"contents": "write"},
        grant_versions={"grant:101": 1, "authorization:101": 1},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    stored = await github_issued_tokens.store_token(
        db_session, token_id=row.token_id, token="secret", fernet=fernet
    )
    assert github_issued_tokens.decrypt_issued_token(stored, fernet=fernet) == "secret"
    assert await github_issued_tokens.select_stale_tokens(db_session) == []
    installation = await db_session.get(GitHubAppInstallation, 909)
    assert installation is not None
    installation.github_repo_full_names = []
    await db_session.flush()
    assert [
        item.token_id for item in await github_issued_tokens.select_stale_tokens(db_session)
    ] == [row.token_id]
    installation.github_repo_full_names = None
    await db_session.flush()
    grant = await db_session.get(AgentGitHubGrant, (tenant_id, agent_id, 101))
    assert grant is not None
    grant.version = 2
    await db_session.flush()
    assert [
        item.token_id
        for item in await github_issued_tokens.select_stale_tokens(
            db_session, include_live_version_bumps=True
        )
    ] == [row.token_id]
    grant.version = 1
    authorization = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert authorization is not None
    authorization.version = 2
    await db_session.flush()
    assert [
        item.token_id
        for item in await github_issued_tokens.select_stale_tokens(
            db_session, include_live_version_bumps=True
        )
    ] == [row.token_id]
    # A delivered token remains usable after a version bump until the vault
    # receives its replacement. The old sweep revoked it before that swap.
    await github_issued_tokens.mark_delivered(db_session, token_id=row.token_id)
    await db_session.flush()
    assert await github_issued_tokens.select_stale_tokens(db_session) == []
    assert await github_issued_tokens.session_token_needs_rotation(db_session, session_id="session")
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
        permissions={"contents": "write"},
        expires_at=row.expires_at,
        grant_versions=row.grant_versions,
        turn_origin_id=origin_id,
    )
    assert event is not None and event.operation == "github_token_mint"
    assert event.github_token_id == row.token_id
    assert event.github_session_id == "session"
    assert event.github_installation_id == 909
    assert event.github_repo_ids == [101]
    assert event.github_permissions == {"contents": "write"}
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
