"""Panel staging, PAT retirement and new-repo card claims."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import (
    Account,
    AgentFile,
    AgentGithubBinding,
    AgentRepoBinding,
    AgentSkillRepoCredential,
    GitHubCredential,
    GitHubNewRepoNotice,
    PlatformPrincipal,
    Tenant,
    TenantAccessPolicyRecord,
    TenantGitHubRepo,
)
from daimon.core.config import Settings
from daimon.core.github_new_repo_delivery import poll_new_repo_notices_once
from daimon.core.github_panel import (
    activate_grants,
    connect_link,
    load_grants_panel,
    pending_connect_link,
    remove_panel_grant,
    stage_panel_grant,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.github_access_requests import get_request, request_access
from daimon.core.stores.github_connect import digest, get_invitation
from daimon.core.stores.github_connected_repos import (
    disconnect_github,
    disconnect_repo,
    set_repo_ability,
)
from daimon.core.stores.github_connected_repos import (
    summary as connected_summary,
)
from daimon.core.stores.github_new_repo_notices import (
    NewRepoNoticeGroup,
    claim_next_notice,
    claim_notice_group,
    dismiss_notice,
    finish_notice,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _setup(
    session: AsyncSession, *, ma_agent_id: str | None = None, saved_state: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    agent_id = (
        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        if ma_agent_id is not None
        else uuid.uuid4()
    )
    session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await session.flush()
    await github_app_installations.upsert(
        session,
        installation_id=999,
        account_login="example",
        repo_full_names=["example/working", "example/other"],
    )
    for repo_id, name in ((101, "example/working"), (102, "example/other")):
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=1,
                installation_id=999,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
    if saved_state:
        session.add(
            AgentRepoBinding(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="example/working",
                default_branch="main",
                ma_secret_ref="inline-pat:test",
            )
        )
        session.add(
            GitHubCredential(
                principal_id=agent_id,
                github_login="example",
                encrypted_token=b"secret",
                scopes=[],
            )
        )
        session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            session.add(
                AgentFile(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    key=key,
                    content="secret",
                    encoding="plain",
                )
            )
    await session.flush()
    return tenant_id, agent_id


@pytest.mark.asyncio
async def test_panel_activation_refuses_saved_key(db_session: AsyncSession) -> None:
    tenant_id, agent_id = await _setup(db_session, ma_agent_id="ag_helper")
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=None,
        is_working_repo=True,
    )
    with pytest.raises(ValueError, match="This agent uses a saved GitHub key"):
        await activate_grants(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            agent_name="Helper",
        )
    assert await db_session.get(GitHubCredential, agent_id) is not None
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )


@pytest.mark.asyncio
async def test_app_mode_grant_removal_allows_existing_working_repo(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = await _setup(db_session, saved_state=False)
    admin_id = uuid.uuid4()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=admin_id,
        is_working_repo=True,
    )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="Helper",
    )
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=102,
        baseline_access="read",
        ceiling_access="read",
        account_id=admin_id,
        is_working_repo=False,
    )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="Helper",
    )
    db_session.add(
        AgentRepoBinding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="example/working",
            default_branch="main",
            ma_secret_ref="old-binding",
        )
    )
    db_session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
    db_session.add(
        AgentFile(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="GH_TOKEN",
            content="saved",
            encoding="plain",
        )
    )
    await db_session.flush()
    await remove_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=102,
        account_id=admin_id,
    )
    await activate_grants(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=admin_id,
        agent_name="Helper",
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert [grant.repo_id for grant in grants] == [101]
    assert await db_session.get(AgentGithubBinding, agent_id) is not None
    assert await db_session.get(AgentFile, (tenant_id, agent_id, "GH_TOKEN")) is not None


@pytest.mark.parametrize("saved_kind", ["pat", "env", "working", "skill", "channel_pin"])
@pytest.mark.asyncio
async def test_panel_activation_refuses_each_saved_state(
    db_session: AsyncSession,
    saved_kind: str,
) -> None:
    tenant_id, agent_id = await _setup(db_session, saved_state=False)
    if saved_kind == "pat":
        db_session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
    elif saved_kind == "env":
        db_session.add(
            AgentFile(
                tenant_id=tenant_id,
                agent_id=agent_id,
                key="GH_TOKEN",
                content="saved",
                encoding="plain",
            )
        )
    elif saved_kind == "working":
        db_session.add(
            AgentRepoBinding(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="example/working",
                default_branch="main",
                ma_secret_ref="saved",
            )
        )
    elif saved_kind == "skill":
        db_session.add(
            AgentSkillRepoCredential(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="example/working",
                default_branch="main",
                ma_secret_ref="saved",
                proof_kind="private",
            )
        )
    else:
        db_session.add(
            TenantAccessPolicyRecord(
                tenant_id=tenant_id,
                policy={"agent_rules": {"Helper": {"runs_in": ["client-channel"]}}},
            )
        )
    await db_session.flush()
    panel = await load_grants_panel(
        db_session, tenant_id=tenant_id, agent_id=agent_id, agent_name="Helper"
    )
    assert panel.saved_state
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=None,
        is_working_repo=False,
    )
    with pytest.raises(ValueError, match="This agent uses a saved GitHub key"):
        await activate_grants(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            agent_name="Helper",
        )
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )


@pytest.mark.asyncio
async def test_server_repo_settings_clamp_live_access_and_disconnect(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = await _setup(db_session, saved_state=False)
    admin_id, member_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(Account(id=member_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=101,
        baseline_access="write",
        ceiling_access="write",
        account_id=admin_id,
        is_working_repo=True,
    )
    await activate_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id, account_id=admin_id, agent_name="Helper"
    )
    overview = await connected_summary(db_session, tenant_id=tenant_id)
    assert overview.count == 2 and overview.owners == ("example",)
    assert overview.agent_count == 1
    with pytest.raises(ValueError, match="Only a server"):
        await set_repo_ability(
            db_session, tenant_id=tenant_id, repo_id=101, ability="read", account_id=member_id
        )
    await set_repo_ability(
        db_session, tenant_id=tenant_id, repo_id=101, ability="read", account_id=admin_id
    )
    with pytest.raises(ValueError, match="Confirm read and write access on GitHub first"):
        await set_repo_ability(
            db_session,
            tenant_id=tenant_id,
            repo_id=101,
            ability="write",
            account_id=admin_id,
        )
    live = await github_access.list_live_grant_repositories(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert len(live) == 1
    assert live[0][0].baseline_access == "read"
    assert live[0][0].ceiling_access == "read"
    await disconnect_repo(db_session, tenant_id=tenant_id, repo_id=101, account_id=admin_id)
    assert (
        await github_access.list_live_grant_repositories(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
        == []
    )
    assert (await connected_summary(db_session, tenant_id=tenant_id)).count == 1


@pytest.mark.asyncio
async def test_disconnect_github_cancels_requests_but_keeps_saved_key(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = await _setup(db_session)
    admin_id, asker_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    waiting = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/working",
        requested_work=None,
        is_admin=False,
    )
    with pytest.raises(ValueError, match="Only a server"):
        await disconnect_github(db_session, tenant_id=tenant_id, account_id=asker_id)
    assert await disconnect_github(db_session, tenant_id=tenant_id, account_id=admin_id) == 2
    assert (await connected_summary(db_session, tenant_id=tenant_id)).count == 0
    assert (
        await get_request(db_session, tenant_id=tenant_id, request_id=waiting.id)
    ).status == "cancelled"  # type: ignore[union-attr]
    assert await db_session.get(GitHubCredential, agent_id) is not None


@pytest.mark.asyncio
async def test_new_repo_card_claim_release_and_one_delivery(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    now = datetime.now(UTC)
    db_session.add(
        GitHubNewRepoNotice(
            tenant_id=tenant_id,
            installation_id=999,
            repo_full_name="example/new",
            queued_at=now - timedelta(days=1),
        )
    )
    await db_session.flush()
    claim = await claim_next_notice(db_session, tenant_id=tenant_id, now=now)
    assert claim is not None and claim.repo_full_name == "example/new"
    assert await claim_next_notice(db_session, tenant_id=tenant_id, now=now) is None
    assert await finish_notice(db_session, notice=claim, delivered=False, now=now)
    again = await claim_next_notice(db_session, tenant_id=tenant_id, now=now)
    assert again is not None
    assert await finish_notice(db_session, notice=again, delivered=True, now=now)
    assert await claim_next_notice(db_session, tenant_id=tenant_id, now=now) is None
    assert await dismiss_notice(
        db_session,
        tenant_id=tenant_id,
        installation_id=999,
        repo_full_name="example/new",
        now=now,
    )


@pytest.mark.asyncio
async def test_new_repo_cards_group_one_day_and_release_each_on_failure(
    db_session: AsyncSession,
) -> None:
    tenant_id, _ = await _setup(db_session)
    now = datetime.now(UTC)
    for name in ("example/first", "example/second"):
        db_session.add(
            GitHubNewRepoNotice(
                tenant_id=tenant_id,
                installation_id=999,
                repo_full_name=name,
                queued_at=now - timedelta(days=1),
            )
        )
    await db_session.flush()
    group = await claim_notice_group(db_session, tenant_id=tenant_id, now=now)
    assert group is not None and set(group.repo_names) == {"example/first", "example/second"}
    assert await claim_notice_group(db_session, tenant_id=tenant_id, now=now) is None
    for notice in group.notices:
        assert await finish_notice(db_session, notice=notice, delivered=False, now=now)
    again = await claim_notice_group(db_session, tenant_id=tenant_id, now=now)
    assert again is not None and len(again.notices) == 2
    for notice in again.notices:
        assert await finish_notice(db_session, notice=notice, delivered=True, now=now)
    assert await claim_notice_group(db_session, tenant_id=tenant_id, now=now) is None


@pytest.mark.asyncio
async def test_new_repo_poller_delivers_daily_group_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _ = await _setup(db_session)
    now = datetime.now(UTC)
    for name in ("example/first", "example/second"):
        db_session.add(
            GitHubNewRepoNotice(
                tenant_id=tenant_id,
                installation_id=999,
                repo_full_name=name,
                queued_at=now - timedelta(days=1),
            )
        )
    await db_session.flush()
    posted: list[tuple[str, ...]] = []

    async def deliver(group: object) -> bool:
        assert isinstance(group, NewRepoNoticeGroup)
        posted.append(group.repo_names)
        return True

    assert (
        await poll_new_repo_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 1
    )
    assert (
        await poll_new_repo_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 0
    )
    assert len(posted) == 1 and set(posted[0]) == {"example/first", "example/second"}


@pytest.mark.asyncio
async def test_new_repo_poller_retries_after_no_dm_lands(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _ = await _setup(db_session)
    db_session.add(
        GitHubNewRepoNotice(
            tenant_id=tenant_id,
            installation_id=999,
            repo_full_name="example/new",
            queued_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.flush()
    attempts = 0

    async def deliver(_group: NewRepoNoticeGroup) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    assert (
        await poll_new_repo_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 0
    )
    assert (
        await poll_new_repo_notices_once(db_session_factory, platform="discord", deliver=deliver)
        == 1
    )
    assert attempts == 2


@pytest.mark.asyncio
async def test_new_repo_notices_wait_to_group_full_day(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    now = datetime.now(UTC)
    for index in range(12):
        db_session.add(
            GitHubNewRepoNotice(
                tenant_id=tenant_id,
                installation_id=999,
                repo_full_name=f"example/new-{index}",
                queued_at=now,
            )
        )
    await db_session.flush()
    assert await claim_notice_group(db_session, tenant_id=tenant_id, now=now) is None
    tomorrow = now + timedelta(days=1)
    group = await claim_notice_group(db_session, tenant_id=tenant_id, now=tomorrow)
    assert group is not None and len(group.repo_names) == 12


@pytest.mark.asyncio
async def test_connection_invitation_names_clicker_and_preselects_notice(
    db_session: AsyncSession,
) -> None:
    tenant_id, _ = await _setup(db_session)
    clicker_id = uuid.uuid4()
    db_session.add(Account(id=clicker_id, tenant_id=tenant_id, role="admin"))
    db_session.add(
        PlatformPrincipal(
            tenant_id=tenant_id,
            platform="discord",
            external_id="clicker",
            account_id=clicker_id,
        )
    )
    await db_session.flush()
    key = SecretStr(Fernet.generate_key().decode())
    settings = cast(
        Settings,
        SimpleNamespace(
            github_app=SimpleNamespace(
                app_id="app",
                app_slug="app",
                private_key="key",
                client_id="client",
                client_secret="secret",
            ),
            mcp=SimpleNamespace(app_root_url="https://example.invalid"),
            crypto=SimpleNamespace(keys=(key,)),
        ),
    )
    member_id = uuid.uuid4()
    db_session.add(Account(id=member_id, tenant_id=tenant_id, role="user"))
    db_session.add(
        PlatformPrincipal(
            tenant_id=tenant_id,
            platform="discord",
            external_id="channel-admin",
            account_id=member_id,
        )
    )
    await db_session.flush()
    with pytest.raises(ValueError, match="workspace admin"):
        await connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="channel-admin",
            verified_tenant_admin=False,
        )
    assert (await db_session.get(Account, member_id)).role == "user"
    with pytest.raises(ValueError, match="workspace admin"):
        await sync_connect_admin(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="channel-admin",
            verified_tenant_admin=False,
        )
    assert (await db_session.get(Account, member_id)).role == "user"
    await sync_connect_admin(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="channel-admin",
        verified_tenant_admin=True,
    )
    assert (await db_session.get(Account, member_id)).role == "admin"
    await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="channel-admin",
        verified_tenant_admin=True,
    )
    with pytest.raises(ValueError, match="workspace admin"):
        await connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="clicker",
            verified_tenant_admin=False,
        )
    url = await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="clicker",
        verified_tenant_admin=True,
    )
    invitation = await get_invitation(db_session, digest(url.rsplit("/", 1)[-1]))
    assert invitation is not None
    assert invitation.requester_label == "clicker"
    grouped_url = await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="clicker",
        verified_tenant_admin=True,
    )
    grouped = await get_invitation(db_session, digest(grouped_url.rsplit("/", 1)[-1]))
    assert grouped is not None
    pending = await pending_connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        requester_account_id=grouped.requester_account_id,
    )
    assert pending == grouped_url
    restarted_url = await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="clicker",
        verified_tenant_admin=True,
        start_over=True,
    )
    assert restarted_url != grouped_url
    assert await get_invitation(db_session, digest(grouped_url.rsplit("/", 1)[-1])) is None
    assert (
        await pending_connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            requester_account_id=grouped.requester_account_id,
        )
        == restarted_url
    )
    agent_id = uuid.uuid4()
    db_session.add(
        AgentFile(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="GH_TOKEN",
            content="saved",
            encoding="plain",
        )
    )
    await db_session.flush()
    with pytest.raises(
        ValueError, match="This agent uses a saved GitHub key. Ask your Daimon operator"
    ):
        await connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="clicker",
            verified_tenant_admin=True,
            agent_id=agent_id,
            agent_name="ResearchBot",
        )
