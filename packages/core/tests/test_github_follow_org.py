"""An agent that follows its GitHub org gets each new repo of the org, and nothing else."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core._models import (
    AgentGitHubGrant,
    GitHubAppInstallation,
    GitHubInstallationReconciliation,
    OpsEvent,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings
from daimon.core.features.snapshot import CACHE
from daimon.core.github_installation_reconcile import (
    FOLLOW_REFRESH_EVERY,
    drain_github_installation_reconciliations,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ops_events import drain_ops_events
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.github_follow_org import FEATURE_KEY
from daimon.core.stores.github_installation_reconciliation import enqueue_following_refreshes
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

INSTALLATION = 7001
EXISTING = ("acme/one", 1)
FOLLOWER, BYSTANDER, CLIENT = "agent_follower", "agent_bystander", "agent_client"
WRITE = {"metadata": "read", "contents": "write", "issues": "write", "pull_requests": "write"}


def _app() -> GithubAppSettings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return GithubAppSettings(app_id="123456", private_key=SecretStr(pem.decode()))


def _github(
    repos: dict[str, int], *, selection: str = "all", permissions: dict[str, str] | None = None
) -> httpx.AsyncClient:
    """GitHub as the worker sees it: the installation and its repository page, with ids."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/app/installations/{INSTALLATION}":
            return httpx.Response(
                200,
                json={
                    "id": INSTALLATION,
                    "account": {"id": 9, "login": "acme", "type": "Organization"},
                    "repository_selection": selection,
                    "permissions": WRITE if permissions is None else permissions,
                },
            )
        if request.url.path == f"/app/installations/{INSTALLATION}/access_tokens":
            return httpx.Response(201, json={"token": "listing"})
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": len(repos),
                    "repositories": [
                        {"id": repo_id, "full_name": name, "owner": {"id": 9}}
                        for name, repo_id in repos.items()
                    ],
                },
            )
        raise AssertionError(f"unexpected GitHub API request: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.tenant_id: uuid.UUID
        self.admin_id: uuid.UUID

    def agent(self, ma_id: str) -> uuid.UUID:
        return derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=ma_id)

    async def grants(self, ma_id: str) -> dict[int, tuple[str, str]]:
        async with self.sessions() as session:
            rows = await github_access.list_agent_grants(
                session, tenant_id=self.tenant_id, agent_id=self.agent(ma_id)
            )
        return {row.repo_id: (row.baseline_access, row.ceiling_access) for row in rows}

    async def repo_rows(self, repo_id: int) -> list[TenantGitHubRepo]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(TenantGitHubRepo).where(TenantGitHubRepo.repo_id == repo_id)
                )
            )

    async def refresh(self, repos: dict[str, int], **github: object) -> int:
        """Run one scheduler drain against GitHub listing `repos`."""
        async with _github(repos, **github) as client:  # type: ignore[arg-type]
            return await drain_github_installation_reconciliations(
                sessionmaker=self.sessions, http_client=client, github_app=_app()
            )

    async def due_again(self) -> None:
        """Put the installation's last read an hour and a bit back."""
        async with self.sessions.begin() as session:
            row = await session.get(GitHubAppInstallation, INSTALLATION)
            assert row is not None and row.github_refreshed_at is not None
            row.github_refreshed_at -= FOLLOW_REFRESH_EVERY + timedelta(minutes=1)


async def _world(sessions: async_sessionmaker[AsyncSession], *, selection: str = "all") -> World:
    """An org install covering all repos, one repo each agent holds, one agent following."""
    world = World(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        world.tenant_id, world.admin_id = tenant.id, account.id
        name, repo_id = EXISTING
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant.id,
                repo_id=repo_id,
                scope_agent_id=None,
                owner_id=9,
                installation_id=INSTALLATION,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=55,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=INSTALLATION,
            account_id=9,
            account_login="acme",
            account_type="Organization",
            repository_selection="all",
            suspended_at=None,
        )
        installation = await session.get(GitHubAppInstallation, INSTALLATION)
        assert installation is not None
        installation.repository_selection = selection  # type: ignore[assignment]
        installation.github_repo_full_names = [name]
        installation.github_refreshed_at = datetime.now(UTC) - timedelta(days=1)
        for ma_id in (FOLLOWER, BYSTANDER, CLIENT):
            await github_access.stage_grant(
                session,
                tenant_id=tenant.id,
                agent_id=world.agent(ma_id),
                repo_id=repo_id,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=None,
            )
        await set_feature_value(
            session,
            tenant_id=tenant.id,
            target=FeatureTarget("agent", agent_id=FOLLOWER),
            key=FEATURE_KEY,
            value="on",
            set_by=account.id,
            surface="web",
        )
    CACHE.clear()
    return world


ONE = dict([EXISTING])
TWO = {**ONE, "acme/two": 2}


async def test_a_new_repo_goes_to_the_following_agent_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)

    assert await world.refresh(TWO) == 1

    assert await world.grants(FOLLOWER) == {1: ("write", "write"), 2: ("write", "write")}
    assert await world.grants(BYSTANDER) == {1: ("write", "write")}
    assert await world.grants(CLIENT) == {1: ("write", "write")}
    (row,) = await world.repo_rows(2)
    assert row.scope_agent_id == world.agent(FOLLOWER), "the row is the agent's own"
    assert (row.status, row.installation_id, row.owner_id) == ("active", INSTALLATION, 9)
    assert row.authorized_by_account_id == world.admin_id, "the admin who turned it on"
    async with db_session_factory() as session:
        grant = await session.get(AgentGitHubGrant, (world.tenant_id, world.agent(FOLLOWER), 2))
        installation = await session.get(GitHubAppInstallation, INSTALLATION)
    assert grant is not None and grant.granted_by_account_id == world.admin_id
    assert installation is not None
    assert installation.github_repo_full_names == ["acme/one", "acme/two"]
    assert installation.repo_full_names == ["acme/one", "acme/two"]


async def test_the_second_refresh_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    await world.refresh(TWO)
    (before,) = await world.repo_rows(2)
    await world.due_again()

    await world.refresh(TWO)

    (after,) = await world.repo_rows(2)
    assert (after.version, after.authorized_at) == (before.version, before.authorized_at)
    assert len(await world.grants(FOLLOWER)) == 2


async def test_a_repo_taken_off_the_agent_is_not_given_back(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    await world.refresh(TWO)
    async with db_session_factory.begin() as session:
        await github_access.lock_tenant_grants(session, tenant_id=world.tenant_id)
        await github_access.remove_grant(
            session, tenant_id=world.tenant_id, agent_id=world.agent(FOLLOWER), repo_id=2
        )
    await world.due_again()
    async with db_session_factory.begin() as session:
        row = await session.get(GitHubAppInstallation, INSTALLATION)
        assert row is not None
        row.github_repo_full_names = [EXISTING[0]]  # the snapshot lags: repo 2 reads as new

    await world.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1}


async def test_an_agent_that_does_not_follow_gets_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_feature_value(
            session,
            tenant_id=world.tenant_id,
            target=FeatureTarget("agent", agent_id=FOLLOWER),
            key=FEATURE_KEY,
            value="off",
            set_by=world.admin_id,
            surface="web",
        )
    CACHE.clear()

    await world.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1}
    assert await world.repo_rows(2) == []


async def test_a_repo_that_is_already_in_the_snapshot_is_not_new(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Turning the feature on gives nothing from before: the repos already listed stay put."""
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        row = await session.get(GitHubAppInstallation, INSTALLATION)
        assert row is not None
        row.github_repo_full_names = ["acme/one", "acme/two"]

    await world.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1}


async def test_a_selected_repos_installation_is_never_followed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory, selection="selected")

    await world.refresh(TWO, selection="selected")

    assert set(await world.grants(FOLLOWER)) == {1}


async def test_a_renamed_repo_keeps_its_row_and_grant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    await world.refresh(TWO)
    await world.due_again()

    await world.refresh({"acme/one": 1, "acme/second": 2})

    (row,) = await world.repo_rows(2)
    assert row.repo_full_name == "acme/second"
    assert set(await world.grants(FOLLOWER)) == {1, 2}


async def test_an_app_without_write_gives_read_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)

    await world.refresh(TWO, permissions={"metadata": "read", "contents": "read"})

    assert (await world.grants(FOLLOWER))[2] == ("read", "read")
    (row,) = await world.repo_rows(2)
    assert row.max_access == "read"


async def test_an_agent_at_its_limit_gets_no_more_and_admins_see_an_issue(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    # The agent already holds one repo; with a token that takes one, it is full.
    monkeypatch.setattr(github_access, "REPOS_PER_INSTALLATION_TOKEN", 1)

    await world.refresh(TWO)
    await drain_ops_events()

    assert set(await world.grants(FOLLOWER)) == {1}, "nothing past the limit"
    assert await world.repo_rows(2) == [], "no row without a grant"
    async with db_session_factory() as session:
        events = list(await session.scalars(select(OpsEvent)))
    assert [(e.code, e.tenant_id) for e in events] == [("github.follow_limit", world.tenant_id)]
    async with db_session_factory() as session:
        job = await session.get(GitHubInstallationReconciliation, INSTALLATION)
    assert job is not None and job.state == "done", "the refresh itself still finishes"


async def test_a_followed_installation_is_read_again_every_hour_and_only_then(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _world(db_session_factory)  # read a day ago, nothing queued

    async def queued(*, older_than: timedelta) -> int:
        async with db_session_factory.begin() as session:
            return await enqueue_following_refreshes(
                session, now=datetime.now(UTC), older_than=older_than, limit=10
            )

    assert await queued(older_than=timedelta(days=2)) == 0, "read more recently than that"
    assert await queued(older_than=FOLLOW_REFRESH_EVERY) == 1
    assert await queued(older_than=FOLLOW_REFRESH_EVERY) == 0, "a pending job counts as queued"


async def test_an_installation_nobody_follows_is_not_read_on_a_timer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_feature_value(
            session,
            tenant_id=world.tenant_id,
            target=FeatureTarget("agent", agent_id=FOLLOWER),
            key=FEATURE_KEY,
            value="off",
            set_by=world.admin_id,
            surface="web",
        )
        session.add(
            GitHubInstallationReconciliation(
                installation_id=INSTALLATION, generation=1, state="done", attempts=1
            )
        )

    async with db_session_factory.begin() as session:
        assert (
            await enqueue_following_refreshes(
                session, now=datetime.now(UTC), older_than=FOLLOW_REFRESH_EVERY, limit=10
            )
            == 0
        )
