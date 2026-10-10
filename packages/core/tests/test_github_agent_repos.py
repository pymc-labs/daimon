"""Repos connected for one agent: who may connect them, and that no other agent gets them."""

from __future__ import annotations

import uuid
from typing import Literal

import pytest
from daimon.core._models import (
    Account,
    AgentFile,
    AgentGithubBinding,
    AgentGitHubGrant,
    AgentRepoBinding,
    AgentSkillRepoCredential,
    GitHubConnectInvitation,
    PlatformPrincipal,
    TenantGitHubRepo,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_admins import ChannelAdminCaller, GroupLookupFailed, GroupMembers
from daimon.core.github_panel import can_manage_agent_github, requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import (
    agent_repo_binding,
    github_access,
    github_app_installations,
    github_connect,
)
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import delete_channel_admins, set_channel_admins
from daimon.core.stores.github_connected_repos import agent_repo_summary
from daimon.core.stores.github_panel_grants import (
    activate_grants,
    load_grants_panel,
    stage_panel_grant,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT = DeploymentDefault()
INSTALLATION = 77
REPOS = {101: "team-a/app", 102: "team-a/docs", 103: "team-b/app"}


class World:
    def __init__(self, tenant_id: uuid.UUID, admin_id: uuid.UUID, channel_admin_id: uuid.UUID):
        self.tenant_id = tenant_id
        self.admin_id = admin_id
        self.channel_admin_id = channel_admin_id

    def agent(self, ma_agent_id: str) -> uuid.UUID:
        return derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=ma_agent_id)


async def _world(db_session: AsyncSession) -> World:
    """TeamA is pinned to #team-a, whose channel admin is user u1; OtherBot runs elsewhere."""
    tenant = await make_tenant(db_session, workspace_id=f"guild-{uuid.uuid4().hex[:8]}")
    admin_id, channel_admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Account(id=admin_id, tenant_id=tenant.id, role="admin"))
    db_session.add(Account(id=channel_admin_id, tenant_id=tenant.id, role="user"))
    await db_session.flush()
    db_session.add(
        PlatformPrincipal(
            tenant_id=tenant.id, platform="discord", external_id="u1", account_id=channel_admin_id
        )
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="team-a",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            agent_channel_pins={"TeamA": ("team-a",), "OtherBot": ("team-b",)}
        ),
    )
    await github_app_installations.upsert(
        db_session,
        installation_id=INSTALLATION,
        account_login="team-a",
        repo_full_names=list(REPOS.values()),
    )
    await db_session.flush()
    return World(tenant.id, admin_id, channel_admin_id)


async def _may_manage(
    db_session: AsyncSession,
    world: World,
    *,
    agent_name: str,
    ma_agent_id: str,
    managed: bool = False,
    user_id: str = "u1",
) -> bool:
    return await can_manage_agent_github(
        db_session,
        tenant_id=world.tenant_id,
        platform="discord",
        caller=ChannelAdminCaller(platform_user_id=user_id),
        agent_names=(agent_name,),
        ma_agent_id=ma_agent_id,
        is_daimon_managed=managed,
        default=DEFAULT,
    )


def _confirmation(
    repo_id: int, access: Literal["read", "write"] = "write"
) -> github_connect.RepoConfirmation:
    return github_connect.RepoConfirmation(
        repo_id=repo_id,
        owner_id=55,
        installation_id=INSTALLATION,
        full_name=REPOS[repo_id],
        max_access=access,
    )


async def _connect(
    db_session: AsyncSession,
    world: World,
    *,
    requester_id: uuid.UUID,
    agent_name: str,
    ma_agent_id: str,
    repo_ids: tuple[int, ...],
    manages: bool,
    access: Literal["read", "write"] = "write",
    manages_at_confirm: bool | None = None,
) -> github_connect.ConfirmedActivation | None:
    """Mint a link for the agent, confirm the repos, then switch the agent to them."""
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=world.tenant_id,
        requester_account_id=requester_id,
        requester_platform_user_id="u1",
        agent_id=world.agent(ma_agent_id),
        agent_name=agent_name,
        agent_ma_id=ma_agent_id,
        agent_manager_verified=manages,
        origin_platform="discord",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.agent_ma_id == ma_agent_id
    state = uuid.uuid4().hex
    await github_connect.create_flow(
        db_session,
        invitation_hash=invitation.token_hash,
        state=state,
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    if not await github_connect.confirm(
        db_session,
        state=state,
        cookie="cookie",
        github_user_id=17,
        repos=[_confirmation(repo_id, access) for repo_id in repo_ids],
        requester_manages_agent=manages if manages_at_confirm is None else manages_at_confirm,
    ):
        return None
    return await github_connect.activate_confirmed_agent(
        db_session,
        invitation=invitation,
        repos=[_confirmation(repo_id, access) for repo_id in repo_ids],
    )


async def _rows(db_session: AsyncSession, world: World, repo_id: int) -> list[TenantGitHubRepo]:
    return list(
        await db_session.scalars(
            select(TenantGitHubRepo).where(
                TenantGitHubRepo.tenant_id == world.tenant_id, TenantGitHubRepo.repo_id == repo_id
            )
        )
    )


async def _server_wide(db_session: AsyncSession, world: World, repo_id: int) -> None:
    db_session.add(
        TenantGitHubRepo(
            tenant_id=world.tenant_id,
            repo_id=repo_id,
            owner_id=55,
            installation_id=INSTALLATION,
            repo_full_name=REPOS[repo_id],
            max_access="write",
            authorized_by_github_user_id=17,
            authorized_by_account_id=world.admin_id,
        )
    )
    await db_session.flush()


async def _live_ids(db_session: AsyncSession, world: World, agent_id: uuid.UUID) -> set[int]:
    return {
        repo.repo_id
        for _, repo in await github_access.list_live_grant_repositories(
            db_session, tenant_id=world.tenant_id, agent_id=agent_id
        )
    }


@pytest.mark.asyncio
async def test_working_repo_selects_only_an_agents_live_repo_and_none_clears(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    agent_id = world.agent("ma_team_a")
    assert await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101, 102),
        manages=True,
    ) == github_connect.ConfirmedActivation(status="activated")
    with pytest.raises(ValueError, match="not on this agent's list"):
        await github_access.set_working_repo(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_name="team-b/app",
            account_id=world.channel_admin_id,
        )
    assert (
        await github_access.set_working_repo(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_name="TEAM-A/APP",
            account_id=world.channel_admin_id,
        )
        == "team-a/app"
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=world.tenant_id, agent_id=agent_id
    )
    assert {grant.repo_id for grant in grants if grant.is_working_repo} == {101}
    assert (
        await github_access.set_working_repo(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_name="team-a/docs",
            account_id=world.channel_admin_id,
        )
        == "team-a/docs"
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=world.tenant_id, agent_id=agent_id
    )
    assert {grant.repo_id for grant in grants if grant.is_working_repo} == {102}
    await agent_repo_binding.set_binding(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=agent_id,
        repo_url="team-a/old",
        default_branch="main",
        ma_secret_ref="legacy",
        proof=None,
    )
    assert (
        await github_access.set_working_repo(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_name=None,
            account_id=world.channel_admin_id,
        )
        is None
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=world.tenant_id, agent_id=agent_id
    )
    assert not any(grant.is_working_repo for grant in grants)
    assert (
        await agent_repo_binding.get_binding(
            db_session, tenant_id=world.tenant_id, agent_id=agent_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_channel_admin_connects_for_their_agent_and_rows_are_scoped(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    assert await _may_manage(db_session, world, agent_name="TeamA", ma_agent_id="ma_team_a")
    team_a = world.agent("ma_team_a")
    result = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )
    assert result == github_connect.ConfirmedActivation(status="activated")
    rows = await _rows(db_session, world, 101)
    assert [(row.scope_agent_id, row.authorized_by_account_id) for row in rows] == [
        (team_a, world.channel_admin_id)
    ]
    assert await github_access.get_agent_mode(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    ) == ("app")
    assert await _live_ids(db_session, world, team_a) == {101}
    listed = await github_access.list_agent_repos(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    )
    assert [(repo.full_name, repo.scope) for repo in listed] == [("team-a/app", "agent")]
    # Nobody else sees it as connected for the server.
    assert await github_access.list_authorized_repos(db_session, tenant_id=world.tenant_id) == []


@pytest.mark.asyncio
async def test_reconnecting_adds_repos_and_keeps_existing_ones(db_session: AsyncSession) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    for repo_ids in ((101,), (102,)):
        assert await _connect(
            db_session,
            world,
            requester_id=world.channel_admin_id,
            agent_name="TeamA",
            ma_agent_id="ma_team_a",
            repo_ids=repo_ids,
            manages=True,
        )
    assert await _live_ids(db_session, world, team_a) == {101, 102}


@pytest.mark.asyncio
async def test_scoped_repo_is_never_granted_or_minted_for_another_agent(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    other = world.agent("ma_other")
    assert await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )
    # Not even a server admin can grant TeamA's repo to another agent.
    with pytest.raises(ValueError, match="not actively authorized"):
        await github_access.stage_grant(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=other,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=world.admin_id,
        )
    # The panel's draft path refuses it for an agent already in App mode.
    await github_access.activate_agent(db_session, tenant_id=world.tenant_id, agent_id=other)
    with pytest.raises(ValueError, match="not connected"):
        await stage_panel_grant(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=other,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            account_id=world.admin_id,
            is_working_repo=False,
        )
    panel = await load_grants_panel(db_session, tenant_id=world.tenant_id, agent_id=other)
    assert panel.repos == ()
    # A grant row written around the stores still never reaches a token.
    db_session.add(
        AgentGitHubGrant(
            tenant_id=world.tenant_id,
            agent_id=other,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            staged=False,
        )
    )
    await db_session.flush()
    assert await _live_ids(db_session, world, other) == set()


@pytest.mark.asyncio
async def test_one_repo_on_two_agents_is_two_rows_and_removal_is_per_agent(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a, other = world.agent("ma_team_a"), world.agent("ma_other")
    for name, ma_agent_id in (("TeamA", "ma_team_a"), ("OtherBot", "ma_other")):
        assert await _connect(
            db_session,
            world,
            requester_id=world.admin_id,
            agent_name=name,
            ma_agent_id=ma_agent_id,
            repo_ids=(101,),
            manages=False,
        )
    assert {row.scope_agent_id for row in await _rows(db_session, world, 101)} == {team_a, other}
    summary = await agent_repo_summary(db_session, tenant_id=world.tenant_id)
    assert (summary.pairs, summary.agents) == (2, 2)
    assert await github_access.remove_agent_repo(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        repo_id=101,
        account_id=world.channel_admin_id,
    )
    assert await _live_ids(db_session, world, team_a) == set()
    assert await _live_ids(db_session, world, other) == {101}
    statuses = {row.scope_agent_id: row.status for row in await _rows(db_session, world, 101)}
    assert statuses == {team_a: "revoked", other: "active"}


@pytest.mark.asyncio
async def test_server_wide_and_scoped_rows_coexist_and_the_agents_own_row_wins(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    loose, other = world.agent("ma_loose"), world.agent("ma_other")
    await _server_wide(db_session, world, 101)
    for agent_id in (loose, other):
        await github_access.stage_grant(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_id=101,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=world.admin_id,
        )
        await github_access.activate_agent(db_session, tenant_id=world.tenant_id, agent_id=agent_id)
    assert await _connect(
        db_session,
        world,
        requester_id=world.admin_id,
        agent_name="Loose",
        ma_agent_id="ma_loose",
        repo_ids=(101,),
        manages=False,
        access="read",
    )
    own = await github_access.repo_for_agent(
        db_session, tenant_id=world.tenant_id, repo_id=101, agent_id=loose
    )
    shared = await github_access.repo_for_agent(
        db_session, tenant_id=world.tenant_id, repo_id=101, agent_id=other
    )
    assert own is not None and own.scope_agent_id == loose and own.max_access == "read"
    assert shared is not None and shared.scope_agent_id is None and shared.max_access == "write"
    [listed] = await github_access.list_agent_repos(
        db_session, tenant_id=world.tenant_id, agent_id=other
    )
    assert listed.scope == "shared"
    # Removing the server-wide repo from OtherBot leaves the row for everyone else.
    await github_access.remove_agent_repo(
        db_session, tenant_id=world.tenant_id, agent_id=other, repo_id=101, account_id=None
    )
    assert shared.status == "active"
    assert await _live_ids(db_session, world, other) == set()
    assert await _live_ids(db_session, world, loose) == {101}


@pytest.mark.asyncio
async def test_channel_admin_cannot_grant_a_server_wide_repo(db_session: AsyncSession) -> None:
    world = await _world(db_session)
    await _server_wide(db_session, world, 102)
    with pytest.raises(ValueError, match="Only a server admin"):
        await github_access.stage_grant(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=world.agent("ma_team_a"),
            repo_id=102,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=world.channel_admin_id,
        )


@pytest.mark.asyncio
async def test_channel_admin_needs_a_local_held_unmanaged_agent(db_session: AsyncSession) -> None:
    world = await _world(db_session)
    assert await _may_manage(db_session, world, agent_name="TeamA", ma_agent_id="ma_team_a")
    # Pinned to a channel they don't administer, or not pinned at all.
    assert not await _may_manage(db_session, world, agent_name="OtherBot", ma_agent_id="ma_other")
    assert not await _may_manage(db_session, world, agent_name="Loose", ma_agent_id="ma_loose")
    # Not a channel admin at all.
    assert not await _may_manage(
        db_session, world, agent_name="TeamA", ma_agent_id="ma_team_a", user_id="u9"
    )
    # A Daimon-managed agent is a server admin's.
    assert not await _may_manage(
        db_session, world, agent_name="TeamA", ma_agent_id="ma_team_a", managed=True
    )
    # Without the check a member's link is refused outright.
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session,
            tenant_id=world.tenant_id,
            requester_account_id=world.channel_admin_id,
            agent_id=world.agent("ma_team_a"),
            agent_name="TeamA",
        )
    # A server-wide link stays admin only even when the caller says they manage an agent.
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session,
            tenant_id=world.tenant_id,
            requester_account_id=world.channel_admin_id,
            agent_manager_verified=True,
        )


@pytest.mark.asyncio
async def test_confirm_refuses_a_requester_who_lost_channel_admin(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)

    async def manages() -> bool:
        return await requester_manages_agent(
            db_session,
            tenant_id=world.tenant_id,
            account_id=world.channel_admin_id,
            platform="discord",
            platform_user_id="u1",
            agent_name="TeamA",
            ma_agent_id="ma_team_a",
            default=DEFAULT,
            is_daimon_managed=False,
            members=None,
        )

    assert await manages()
    await delete_channel_admins(
        db_session,
        tenant_id=world.tenant_id,
        platform="discord",
        channel_id="team-a",
    )
    assert not await manages()
    # The link was made while they managed TeamA; confirming uses the recheck.
    assert (
        await _connect(
            db_session,
            world,
            requester_id=world.channel_admin_id,
            agent_name="TeamA",
            ma_agent_id="ma_team_a",
            repo_ids=(101,),
            manages=True,
            manages_at_confirm=await manages(),
        )
        is None
    )
    assert await _rows(db_session, world, 101) == []
    # A link without the agent's Managed Agents id fails closed.
    assert not await requester_manages_agent(
        db_session,
        tenant_id=world.tenant_id,
        account_id=world.channel_admin_id,
        platform="discord",
        platform_user_id="u1",
        agent_name="TeamA",
        ma_agent_id=None,
        default=DEFAULT,
        is_daimon_managed=False,
        members=None,
    )


async def _saved_key_and_working_repo(
    db_session: AsyncSession, world: World, agent_id: uuid.UUID
) -> None:
    db_session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
    db_session.add(
        AgentFile(
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            key="GH_TOKEN",
            content="encrypted-placeholder",
            encoding="plain",
        )
    )
    db_session.add(
        AgentRepoBinding(
            tenant_id=world.tenant_id,
            agent_id=agent_id,
            repo_url="team-a/app",
            default_branch="main",
            ma_secret_ref="saved-key",
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_saved_key_is_retired_only_when_the_working_repo_stays_covered(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _saved_key_and_working_repo(db_session, world, team_a)
    # The working repo was not ticked: keep the key and say what is missing.
    pending = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(102,),
        manages=True,
    )
    assert pending == github_connect.ConfirmedActivation(
        status="update_pending",
        missing_repos=(github_connect.MissingRepo(full_name="team-a/app", needs_write=True),),
    )
    assert await db_session.get(AgentGithubBinding, team_a) is not None
    assert await github_access.get_agent_mode(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    ) == ("legacy")
    # Connecting it with write covers it, so this Connect retires the saved key.
    done = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )
    assert done == github_connect.ConfirmedActivation(status="activated", retired_saved_key=True)
    assert await db_session.get(AgentGithubBinding, team_a) is None
    assert await db_session.get(AgentFile, (world.tenant_id, team_a, "GH_TOKEN")) is None
    assert await _live_ids(db_session, world, team_a) == {101, 102}


@pytest.mark.asyncio
async def test_read_only_working_repo_keeps_the_key_until_write_is_connected(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _saved_key_and_working_repo(db_session, world, team_a)
    pending = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
        access="read",
    )
    assert pending is not None and pending.status == "update_pending"
    await stage_panel_grant(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        account_id=world.channel_admin_id,
        is_working_repo=True,
    )
    # Read only on the working repo still strands it.
    with pytest.raises(ValueError, match="working repo"):
        await activate_grants(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=team_a,
            account_id=world.channel_admin_id,
            agent_name="TeamA",
        )
    await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )
    assert await github_access.get_agent_mode(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    ) == ("app")
    assert await db_session.get(AgentGithubBinding, team_a) is None
    statuses = set(
        await db_session.scalars(
            select(GitHubConnectInvitation.activation_status).where(
                GitHubConnectInvitation.agent_id == team_a,
                GitHubConnectInvitation.used_at.is_not(None),
            )
        )
    )
    assert statuses == {"update_pending", "activated"}


@pytest.mark.asyncio
async def test_panel_save_retires_the_key_after_a_pending_connect(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _saved_key_and_working_repo(db_session, world, team_a)
    await _server_wide(db_session, world, 102)
    # Without a pending Connect the panel still refuses to switch a saved key.
    await stage_panel_grant(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        repo_id=102,
        baseline_access="read",
        ceiling_access="read",
        account_id=world.admin_id,
        is_working_repo=False,
    )
    with pytest.raises(github_connect.ClientAgentConnectionError):
        await activate_grants(
            db_session,
            tenant_id=world.tenant_id,
            agent_id=team_a,
            account_id=world.admin_id,
            agent_name="Unpinned",
        )
    await github_access.remove_grant(
        db_session, tenant_id=world.tenant_id, agent_id=team_a, repo_id=102
    )
    pending = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
        access="read",
    )
    assert pending is not None and pending.status == "update_pending"
    row = await db_session.scalar(
        select(TenantGitHubRepo).where(
            TenantGitHubRepo.repo_id == 101, TenantGitHubRepo.scope_agent_id == team_a
        )
    )
    assert row is not None
    row.max_access = "write"
    await stage_panel_grant(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=world.channel_admin_id,
        is_working_repo=True,
    )
    assert await activate_grants(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        account_id=world.channel_admin_id,
        agent_name="TeamA",
    )
    assert await db_session.get(AgentGithubBinding, team_a) is None
    assert not await github_connect.has_pending_connect_update(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    )


@pytest.mark.asyncio
async def test_pinned_agent_refuses_a_server_wide_grant_but_takes_its_own(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _server_wide(db_session, world, 102)
    await github_access.stage_grant(
        db_session,
        tenant_id=world.tenant_id,
        agent_id=team_a,
        repo_id=102,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=world.admin_id,
    )
    with pytest.raises(github_connect.ClientAgentConnectionError):
        await github_connect.require_app_eligible_agent(
            db_session, tenant_id=world.tenant_id, agent_id=team_a, agent_name="TeamA"
        )
    await github_access.remove_grant(
        db_session, tenant_id=world.tenant_id, agent_id=team_a, repo_id=102
    )
    assert await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )


@pytest.mark.asyncio
async def test_confirm_recheck_uses_live_roles_and_managed_status(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    # The channel admin holds #team-a only through a Discord role now.
    await set_channel_admins(
        db_session,
        tenant_id=world.tenant_id,
        platform="discord",
        channel_id="team-a",
        role_ids=["role-team-a"],
        user_ids=[],
        actor_account_id=None,
    )
    account = await db_session.get(Account, world.channel_admin_id)
    assert account is not None
    account.platform_role_ids = ["role-team-a"]
    await db_session.flush()

    async def holds(user_id: str) -> frozenset[str]:
        return frozenset({"role-team-a"}) if user_id == "u1" else frozenset()

    async def left(user_id: str) -> frozenset[str]:
        return frozenset()

    async def unreachable(user_id: str) -> frozenset[str]:
        raise GroupLookupFailed("Discord did not answer")

    async def manages(
        members: GroupMembers | None, managed: bool | None = False, user_id: str | None = "u1"
    ) -> bool:
        return await requester_manages_agent(
            db_session,
            tenant_id=world.tenant_id,
            account_id=world.channel_admin_id,
            platform="discord",
            platform_user_id=user_id,
            agent_name="TeamA",
            ma_agent_id="ma_team_a",
            default=DEFAULT,
            is_daimon_managed=managed,
            members=members,
        )

    assert await manages(holds)
    # The stored role alone is not enough: no lookup, a lookup that fails, or
    # a member who left the role is no.
    assert not await manages(None)
    assert not await manages(unreachable)
    assert not await manages(left)
    # Managed status must be known, and a managed agent is a server admin's.
    assert not await manages(holds, managed=None)
    assert not await manages(holds, managed=True)
    # Without the requester's platform ID their stored role can't be checked.
    assert not await manages(holds, user_id=None)


@pytest.mark.asyncio
async def test_reconnecting_at_read_keeps_existing_write(db_session: AsyncSession) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    for access in ("write", "read"):
        assert await _connect(
            db_session,
            world,
            requester_id=world.channel_admin_id,
            agent_name="TeamA",
            ma_agent_id="ma_team_a",
            repo_ids=(101,),
            manages=True,
            access=access,
        )
    [grant] = await github_access.list_agent_grants(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    )
    assert (grant.baseline_access, grant.ceiling_access) == ("write", "write")
    [row] = await _rows(db_session, world, 101)
    assert row.max_access == "write"


@pytest.mark.asyncio
async def test_panel_manages_pinned_agents_own_repos_and_pending_saved_keys(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _server_wide(db_session, world, 102)
    assert await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
    )
    panel = await load_grants_panel(
        db_session, tenant_id=world.tenant_id, agent_id=team_a, agent_name="TeamA"
    )
    # Pinned with only its own repos: the normal controls, and only its own repos.
    assert not panel.saved_state
    assert [repo.full_name for repo in panel.repos] == ["team-a/app"]

    # A saved-key agent waiting on a chat Connect reaches the panel to finish it.
    loose = world.agent("ma_loose")
    await _saved_key_and_working_repo(db_session, world, loose)

    async def loose_panel() -> bool:
        return (
            await load_grants_panel(
                db_session, tenant_id=world.tenant_id, agent_id=loose, agent_name="Loose"
            )
        ).saved_state

    assert await loose_panel()
    pending = await _connect(
        db_session,
        world,
        requester_id=world.admin_id,
        agent_name="Loose",
        ma_agent_id="ma_loose",
        repo_ids=(102,),
        manages=False,
    )
    assert pending is not None and pending.status == "update_pending"
    assert not await loose_panel()


@pytest.mark.asyncio
async def test_panel_sends_a_pinned_agent_with_a_server_wide_grant_to_the_operator(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _server_wide(db_session, world, 102)
    db_session.add(
        AgentGitHubGrant(
            tenant_id=world.tenant_id,
            agent_id=team_a,
            repo_id=102,
            baseline_access="read",
            ceiling_access="read",
        )
    )
    await db_session.flush()
    panel = await load_grants_panel(
        db_session, tenant_id=world.tenant_id, agent_id=team_a, agent_name="TeamA"
    )
    assert panel.saved_state


@pytest.mark.asyncio
async def test_a_repo_that_is_working_and_skill_repo_is_missing_once_with_write(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    team_a = world.agent("ma_team_a")
    await _saved_key_and_working_repo(db_session, world, team_a)
    db_session.add(
        AgentSkillRepoCredential(
            tenant_id=world.tenant_id,
            agent_id=team_a,
            repo_url="team-a/app",
            default_branch="main",
            ma_secret_ref="saved-key",
            proof_kind="private",
        )
    )
    await db_session.flush()
    assert await github_connect.missing_required_repos(
        db_session, tenant_id=world.tenant_id, agent_id=team_a
    ) == [github_connect.MissingRepo(full_name="team-a/app", needs_write=True)]
    # Read only on it still leaves the agent waiting, and says it needs write.
    pending = await _connect(
        db_session,
        world,
        requester_id=world.channel_admin_id,
        agent_name="TeamA",
        ma_agent_id="ma_team_a",
        repo_ids=(101,),
        manages=True,
        access="read",
    )
    assert pending == github_connect.ConfirmedActivation(
        status="update_pending",
        missing_repos=(github_connect.MissingRepo(full_name="team-a/app", needs_write=True),),
    )
