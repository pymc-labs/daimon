"""A legacy agent's working repo survives the switch to GitHub App mode.

App mode ignores the legacy repo binding and mounts only the grant marked as
the working repo, so the switch has to carry the binding over.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import (
    Account,
    AgentRepoBinding,
    GitHubConnectInvitation,
    SecurityAuditEvent,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.github_panel import activate_grants, stage_panel_grant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access, github_app_installations, github_connect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_REPOS = {101: "Example/Work", 102: "example/other"}


async def _tenant(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, admin_id, member_id = (uuid.uuid4() for _ in range(3))
    session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await session.flush()
    session.add_all(
        [
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
            Account(id=member_id, tenant_id=tenant_id, role="user"),
        ]
    )
    await github_app_installations.upsert_github_app(
        session,
        installation_id=77,
        account_id=55,
        account_login="example",
        account_type="Organization",
        repository_selection="selected",
        suspended_at=None,
    )
    await session.flush()
    return tenant_id, admin_id, member_id


async def _bind(session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    # The binding stores the canonical lowercase-insensitive owner/repo form.
    session.add(
        AgentRepoBinding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="example/work",
            default_branch="main",
            ma_secret_ref="legacy-key",
        )
    )
    await session.flush()


async def _connect(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    member_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_ids: tuple[int, ...],
) -> github_connect.ConfirmedActivation:
    """A chat Connect: the person confirms `repo_ids` for the agent, with write."""
    confirmations = [
        github_connect.RepoConfirmation(
            repo_id=repo_id,
            owner_id=55,
            installation_id=77,
            full_name=_REPOS[repo_id],
            max_access="write",
        )
        for repo_id in repo_ids
    ]
    token = await github_connect.mint_invitation(
        session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ResearchBot",
    )
    await github_connect.record_connect_request(
        session,
        tenant_id=tenant_id,
        requester_account_id=member_id,
        agent_id=agent_id,
        agent_name="ResearchBot",
    )
    invitation = await github_connect.get_invitation(session, github_connect.digest(token))
    assert invitation is not None
    await github_connect.create_flow(
        session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.confirm(
        session, state="state", cookie="cookie", github_user_id=17, repos=confirmations
    )
    activation = await github_connect.activate_confirmed_agent(
        session, invitation=invitation, repos=confirmations
    )
    assert activation is not None
    return activation


async def _working(session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> set[int]:
    grants = await github_access.list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    assert all(not grant.staged for grant in grants)
    return {grant.repo_id for grant in grants if grant.is_working_repo}


@pytest.mark.asyncio
async def test_connect_confirm_keeps_the_legacy_working_repo(db_session: AsyncSession) -> None:
    tenant_id, admin_id, member_id = await _tenant(db_session)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent-bound")
    await _bind(db_session, tenant_id, agent_id)
    activation = await _connect(
        db_session,
        tenant_id=tenant_id,
        admin_id=admin_id,
        member_id=member_id,
        agent_id=agent_id,
        repo_ids=(102, 101),
    )
    assert activation.status == "activated"
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "app"
    )
    # The binding is "example/work"; the connected repo is "Example/Work".
    assert await _working(db_session, tenant_id, agent_id) == {101}
    grant = next(
        grant
        for grant in await github_access.list_agent_grants(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
        if grant.repo_id == 101
    )
    # The change bumps the version, so the inventory sweeper sees it.
    assert grant.version >= 3
    reasons = list(
        await db_session.scalars(
            select(SecurityAuditEvent.reason).where(SecurityAuditEvent.agent_id == agent_id)
        )
    )
    assert "working repo kept from legacy binding" in reasons


@pytest.mark.asyncio
async def test_connect_confirm_without_a_binding_marks_no_working_repo(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, member_id = await _tenant(db_session)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent-unbound")
    activation = await _connect(
        db_session,
        tenant_id=tenant_id,
        admin_id=admin_id,
        member_id=member_id,
        agent_id=agent_id,
        repo_ids=(101, 102),
    )
    assert activation.status == "activated"
    assert await _working(db_session, tenant_id, agent_id) == set()


@pytest.mark.asyncio
async def test_connect_confirm_keeps_an_explicit_working_repo(db_session: AsyncSession) -> None:
    tenant_id, admin_id, member_id = await _tenant(db_session)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent-chosen")
    await _bind(db_session, tenant_id, agent_id)
    # The person already picked the other repo as the working repo (staged).
    for repo_id in (101, 102):
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=55,
                installation_id=77,
                repo_full_name=_REPOS[repo_id],
                max_access="write",
                authorized_by_github_user_id=17,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=102,
        baseline_access="write",
        ceiling_access="write",
        granted_by_account_id=admin_id,
        is_working_repo=True,
    )
    await _connect(
        db_session,
        tenant_id=tenant_id,
        admin_id=admin_id,
        member_id=member_id,
        agent_id=agent_id,
        repo_ids=(101, 102),
    )
    assert await _working(db_session, tenant_id, agent_id) == {102}


async def _panel_agent(
    session: AsyncSession, *, bound: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A legacy agent finishing a chat Connect that stayed staged, via the panel."""
    tenant_id, admin_id, _ = await _tenant(session)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent-panel")
    for repo_id, name in _REPOS.items():
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=55,
                installation_id=77,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=17,
                status="active",
                version=1,
            )
        )
    if bound:
        await _bind(session, tenant_id, agent_id)
        session.add(
            GitHubConnectInvitation(
                token_hash="pending-connect",
                tenant_id=tenant_id,
                requester_account_id=admin_id,
                workspace_label="workspace",
                requester_label="admin",
                agent_id=agent_id,
                agent_name="ResearchBot",
                operator_issued=False,
                activation_status="update_pending",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    await session.flush()
    return tenant_id, admin_id, agent_id


@pytest.mark.parametrize("explicit", [None, 102])
@pytest.mark.asyncio
async def test_panel_save_keeps_the_legacy_working_repo(
    db_session: AsyncSession, explicit: int | None
) -> None:
    tenant_id, admin_id, agent_id = await _panel_agent(db_session, bound=True)
    for repo_id in (101, 102):
        await stage_panel_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            baseline_access="write",
            ceiling_access="write",
            account_id=admin_id,
            is_working_repo=repo_id == explicit,
        )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="ResearchBot",
    )
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "app"
    )
    # No explicit choice: the binding's repo. An explicit choice wins over it.
    assert await _working(db_session, tenant_id, agent_id) == {explicit or 101}


@pytest.mark.asyncio
async def test_panel_save_without_a_binding_marks_no_working_repo(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, agent_id = await _panel_agent(db_session, bound=False)
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=admin_id,
        is_working_repo=False,
    )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="ResearchBot",
    )
    assert await _working(db_session, tenant_id, agent_id) == set()


@pytest.mark.asyncio
async def test_a_working_repo_cleared_in_app_mode_stays_cleared(db_session: AsyncSession) -> None:
    tenant_id, admin_id, agent_id = await _panel_agent(db_session, bound=False)
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=admin_id,
        is_working_repo=False,
    )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="ResearchBot",
    )
    # A leftover binding does not bring the working repo back once the agent
    # is already in App mode: only the switch itself carries it over.
    await _bind(db_session, tenant_id, agent_id)
    await github_access.activate_agent(
        db_session, tenant_id=tenant_id, agent_id=agent_id, changed_by_account_id=admin_id
    )
    assert await _working(db_session, tenant_id, agent_id) == set()
