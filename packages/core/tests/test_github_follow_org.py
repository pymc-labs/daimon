"""An agent that follows its GitHub org holds every repo of it, except those taken off."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core._models import (
    AgentGitHubGrant,
    AgentGitHubGrantRemoval,
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
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import FeatureTarget, Role
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.github_follow_org import FEATURE_KEY
from daimon.core.stores.github_installation_reconciliation import enqueue_following_refreshes
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

INSTALLATION = 7001
FOLLOWER, BYSTANDER, CLIENT = "agent_follower", "agent_bystander", "agent_client"
WRITE = {"metadata": "read", "contents": "write", "issues": "write", "pull_requests": "write"}
ONE = {"acme/one": 1}
TWO = {**ONE, "acme/two": 2}


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


class Workspace:
    """One workspace with an admin, repo 1 held by three agents and one agent following."""

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
                    select(TenantGitHubRepo).where(
                        TenantGitHubRepo.tenant_id == self.tenant_id,
                        TenantGitHubRepo.repo_id == repo_id,
                    )
                )
            )

    async def set_follow(self, value: str, ma_id: str = FOLLOWER) -> None:
        async with self.sessions.begin() as session:
            await set_feature_value(
                session,
                tenant_id=self.tenant_id,
                target=FeatureTarget("agent", agent_id=ma_id),
                key=FEATURE_KEY,
                value=value,
                set_by=self.admin_id,
                surface="web",
            )
        CACHE.clear()

    async def connect(self, ma_id: str, repo_id: int, name: str) -> None:
        """What the connect page does for one agent: its own row, the grant, the cache."""
        async with self.sessions.begin() as session:
            agent_id = self.agent(ma_id)
            session.add(
                TenantGitHubRepo(
                    tenant_id=self.tenant_id,
                    repo_id=repo_id,
                    scope_agent_id=agent_id,
                    owner_id=9,
                    installation_id=INSTALLATION,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=55,
                    status="active",
                    version=1,
                )
            )
            installation = await session.get(GitHubAppInstallation, INSTALLATION)
            assert installation is not None
            installation.repo_full_names = sorted({*installation.repo_full_names, name})
            installation.github_repo_full_names = sorted(
                {*(installation.github_repo_full_names or []), name}
            )
            await session.flush()
            await github_access.stage_grant(
                session,
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=self.admin_id,
            )


class Org:
    """The installation and the workspaces that use it; one fake GitHub for all of them."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], workspaces: list[Workspace]
    ) -> None:
        self.sessions = sessions
        self.workspaces = workspaces

    @property
    def one(self) -> Workspace:
        return self.workspaces[0]

    async def refresh(self, repos: dict[str, int], **github: Any) -> int:
        """Run one scheduler drain against GitHub listing `repos`."""
        async with _github(repos, **github) as client:
            return await drain_github_installation_reconciliations(
                sessionmaker=self.sessions, http_client=client, github_app=_app()
            )

    async def due_again(self) -> None:
        """Put the installation's last read an hour and a bit back, and its job at done."""
        async with self.sessions.begin() as session:
            row = await session.get(GitHubAppInstallation, INSTALLATION)
            assert row is not None and row.github_refreshed_at is not None
            row.github_refreshed_at -= FOLLOW_REFRESH_EVERY + timedelta(minutes=1)


async def _org(
    sessions: async_sessionmaker[AsyncSession], *, selection: str = "all", workspaces: int = 1
) -> Org:
    """An org install covering all repos; each workspace has one following agent."""
    made: list[Workspace] = []
    async with sessions.begin() as session:
        for _ in range(workspaces):
            world = Workspace(sessions)
            tenant = await make_tenant(session)
            account = await make_account(session, tenant=tenant)
            await set_role(session, account.id, Role.ADMIN)
            world.tenant_id, world.admin_id = tenant.id, account.id
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant.id,
                    repo_id=1,
                    scope_agent_id=None,
                    owner_id=9,
                    installation_id=INSTALLATION,
                    repo_full_name="acme/one",
                    max_access="write",
                    authorized_by_github_user_id=55,
                    status="active",
                    version=1,
                )
            )
            made.append(world)
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
        installation.github_repo_full_names = ["acme/one"]
        installation.github_refreshed_at = datetime.now(UTC) - timedelta(days=1)
        for world in made:
            for ma_id in (FOLLOWER, BYSTANDER, CLIENT):
                await github_access.stage_grant(
                    session,
                    tenant_id=world.tenant_id,
                    agent_id=world.agent(ma_id),
                    repo_id=1,
                    baseline_access="write",
                    ceiling_access="write",
                    granted_by_account_id=None,
                )
    for world in made:
        await world.set_follow("on")
    return Org(sessions, made)


async def test_a_new_repo_goes_to_the_following_agent_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)
    world = org.one

    assert await org.refresh(TWO) == 1

    assert await world.grants(FOLLOWER) == {1: ("write", "write"), 2: ("write", "write")}
    assert await world.grants(BYSTANDER) == {1: ("write", "write")}
    assert await world.grants(CLIENT) == {1: ("write", "write")}
    (row,) = await world.repo_rows(2)
    assert row.scope_agent_id == world.agent(FOLLOWER), "the row is the agent's own"
    assert (row.status, row.installation_id, row.owner_id) == ("active", INSTALLATION, 9)
    assert row.authorized_by_account_id == world.admin_id, "the admin who turned it on"
    async with db_session_factory() as session:
        grant = await session.get(AgentGitHubGrant, (world.tenant_id, world.agent(FOLLOWER), 2))
    assert grant is not None and grant.granted_by_account_id == world.admin_id


async def test_the_second_refresh_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)
    await org.refresh(TWO)
    (before,) = await org.one.repo_rows(2)
    await org.due_again()

    await org.refresh(TWO)

    (after,) = await org.one.repo_rows(2)
    assert (after.version, after.authorized_at) == (before.version, before.authorized_at)
    assert len(await org.one.grants(FOLLOWER)) == 2


async def test_turning_it_on_gives_what_the_agent_lacks_at_the_next_refresh(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No baseline: a repo that was already listed is given too, as Select all would."""
    org = await _org(db_session_factory)
    world = org.one
    await world.set_follow("off")
    async with db_session_factory.begin() as session:
        row = await session.get(GitHubAppInstallation, INSTALLATION)
        assert row is not None
        row.github_repo_full_names = ["acme/one", "acme/two"]
    await org.refresh(TWO)
    assert set(await world.grants(FOLLOWER)) == {1}, "off: nothing"

    await world.set_follow("on")
    await org.due_again()
    await org.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1, 2}


async def test_a_repo_connected_to_another_agent_first_still_reaches_the_follower(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The review's bounded check: create a repo, connect it to another agent, refresh twice."""
    org = await _org(db_session_factory)
    world = org.one
    await world.connect(BYSTANDER, 2, "acme/two")

    await org.refresh(TWO)
    await org.due_again()
    await org.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1, 2}
    assert set(await world.grants(BYSTANDER)) == {1, 2}
    assert set(await world.grants(CLIENT)) == {1}


async def test_a_repo_taken_off_the_agent_is_not_given_back_until_granted_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)
    world = org.one
    await org.refresh(TWO)
    async with db_session_factory.begin() as session:
        await github_access.lock_tenant_grants(session, tenant_id=world.tenant_id)
        await github_access.remove_grant(
            session, tenant_id=world.tenant_id, agent_id=world.agent(FOLLOWER), repo_id=2
        )
    async with db_session_factory() as session:
        assert await session.get(
            AgentGitHubGrantRemoval, (world.tenant_id, world.agent(FOLLOWER), 2)
        ), "removal leaves a record"
    await org.due_again()

    await org.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1}

    async with db_session_factory.begin() as session:
        await github_access.stage_grant(
            session,
            tenant_id=world.tenant_id,
            agent_id=world.agent(FOLLOWER),
            repo_id=2,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=world.admin_id,
        )
    async with db_session_factory() as session:
        assert not await session.get(
            AgentGitHubGrantRemoval, (world.tenant_id, world.agent(FOLLOWER), 2)
        ), "granting again clears it"


async def test_a_repo_connected_for_everyone_is_granted_without_a_second_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)
    world = org.one
    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=world.tenant_id,
                repo_id=2,
                scope_agent_id=None,
                owner_id=9,
                installation_id=INSTALLATION,
                repo_full_name="acme/two",
                max_access="write",
                authorized_by_github_user_id=55,
                status="active",
                version=1,
            )
        )
        installation = await session.get(GitHubAppInstallation, INSTALLATION)
        assert installation is not None
        installation.repo_full_names = ["acme/one", "acme/two"]

    await org.refresh(TWO)

    assert set(await world.grants(FOLLOWER)) == {1, 2}
    assert len(await world.repo_rows(2)) == 1, "the server-wide row is the one used"


async def test_an_agent_that_does_not_follow_gets_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)
    await org.one.set_follow("off")

    await org.refresh(TWO)

    assert set(await org.one.grants(FOLLOWER)) == {1}
    assert await org.one.repo_rows(2) == []


async def test_a_selected_repos_installation_is_never_followed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory, selection="selected")

    await org.refresh(TWO, selection="selected")

    assert set(await org.one.grants(FOLLOWER)) == {1}


async def test_an_app_without_write_gives_read_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org = await _org(db_session_factory)

    await org.refresh(TWO, permissions={"metadata": "read", "contents": "read"})

    assert (await org.one.grants(FOLLOWER))[2] == ("read", "read")
    (row,) = await org.one.repo_rows(2)
    assert row.max_access == "read"


async def test_the_cap_counts_distinct_repos_and_the_rest_wait_for_room(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(db_session_factory)
    world = org.one
    # Repo 1 has a server-wide row and now an agent row too: still one repo.
    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=world.tenant_id,
                repo_id=1,
                scope_agent_id=world.agent(FOLLOWER),
                owner_id=9,
                installation_id=INSTALLATION,
                repo_full_name="acme/one",
                max_access="write",
                authorized_by_github_user_id=55,
                status="active",
                version=1,
            )
        )
    monkeypatch.setattr(github_access, "REPOS_PER_INSTALLATION_TOKEN", 2)
    three = {**TWO, "acme/three": 3}

    await org.refresh(three)
    await drain_ops_events()

    assert set(await world.grants(FOLLOWER)) == {1, 2}, "room for one more, not two"
    async with db_session_factory() as session:
        events = list(await session.scalars(select(OpsEvent)))
    assert [(e.code, e.tenant_id) for e in events] == [("github.follow_limit", world.tenant_id)]

    monkeypatch.setattr(github_access, "REPOS_PER_INSTALLATION_TOKEN", 3)
    await org.due_again()
    await org.refresh(three)

    assert set(await world.grants(FOLLOWER)) == {1, 2, 3}, "granted once there is room"


async def test_one_workspace_failing_does_not_stop_the_others_and_is_retried(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(db_session_factory, workspaces=2)
    first, second = sorted(org.workspaces, key=lambda w: str(w.tenant_id))
    real = github_access.stage_grant
    broken = {"on": True}

    async def flaky(session: AsyncSession, **kwargs: Any) -> Any:
        if broken["on"] and kwargs["tenant_id"] == second.tenant_id and kwargs["repo_id"] == 2:
            raise RuntimeError("database went away")
        return await real(session, **kwargs)

    monkeypatch.setattr(github_access, "stage_grant", flaky)

    await org.refresh(TWO)

    assert set(await first.grants(FOLLOWER)) == {1, 2}, "the first workspace is done"
    assert set(await second.grants(FOLLOWER)) == {1}
    async with db_session_factory() as session:
        job = await session.get(GitHubInstallationReconciliation, INSTALLATION)
    assert job is not None and job.state == "pending", "another refresh is queued"

    broken["on"] = False
    async with db_session_factory.begin() as session:
        job = await session.get(GitHubInstallationReconciliation, INSTALLATION)
        assert job is not None
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await org.refresh(TWO)

    assert set(await second.grants(FOLLOWER)) == {1, 2}
    assert set(await first.grants(FOLLOWER)) == {1, 2}


async def test_a_followed_installation_is_read_again_every_hour_and_only_then(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _org(db_session_factory)  # read a day ago, nothing queued

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
    org = await _org(db_session_factory)
    await org.one.set_follow("off")
    async with db_session_factory.begin() as session:
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
