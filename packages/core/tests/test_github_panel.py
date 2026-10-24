"""Panel staging, PAT retirement and new-repo card claims."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import (
    Account,
    AgentGitHubGrantDraft,
    GitHubNewRepoNotice,
    PlatformPrincipal,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.github_new_repo_delivery import poll_new_repo_notices_once
from daimon.core.github_panel import (
    activate_grants,
    connect_link,
    pending_connect_link,
    safe_github_error,
    stage_panel_grant,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access, github_app_installations, github_panel_grants
from daimon.core.stores.github_connect import digest, get_invitation
from daimon.core.stores.github_connected_repos import (
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_panel_batch_and_confirmation_serialize_on_tenant_lock(
    db_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        tenant_id, agent_id = await _setup(session)
        for repo_id in (101, 102):
            session.add(
                AgentGitHubGrantDraft(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    operation="upsert",
                    baseline_access="write",
                    ceiling_access="write",
                    is_working_repo=False,
                )
            )
    first_staged, release_first = asyncio.Event(), asyncio.Event()
    real_stage = github_access.stage_grant

    async def paused_stage(*args: object, **kwargs: object) -> object:
        result = await real_stage(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs["repo_id"] == 101:
            first_staged.set()
            await release_first.wait()
        return result

    monkeypatch.setattr(github_access, "stage_grant", paused_stage)

    async def panel() -> None:
        async with factory.begin() as session:
            await github_panel_grants.activate_grants(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                agent_name="Helper",
            )

    async def confirmation() -> None:
        async with factory.begin() as session:
            await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
            await github_access.lock_repo_grants_batch(session, tenant_id=tenant_id, repo_ids={102})
            await github_access.lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)

    panel_task = asyncio.create_task(panel())
    try:
        await asyncio.wait_for(first_staged.wait(), timeout=5)
        confirm_task = asyncio.create_task(confirmation())
        await asyncio.sleep(0.05)
        assert not confirm_task.done()
    finally:
        release_first.set()
    await asyncio.wait_for(asyncio.gather(panel_task, confirm_task), timeout=5)


@pytest.mark.parametrize(
    "error_type",
    [
        github_access.InstallationLimitError,
        github_access.SessionVaultBudgetError,
        github_access.ReadOnlyMultiInstallationError,
    ],
)
def test_grant_admission_error_copy_is_safe_to_show(error_type: type[ValueError]) -> None:
    message = "Remove a repo or an account first."
    assert safe_github_error(error_type(message)) == message


async def _setup(
    session: AsyncSession, *, ma_agent_id: str | None = None
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
    await session.flush()
    return tenant_id, agent_id


@pytest.mark.asyncio
async def test_server_repo_settings_clamp_live_access_and_disconnect(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = await _setup(db_session)
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
    with pytest.raises(ValueError, match="Only an admin"):
        await set_repo_ability(
            db_session, tenant_id=tenant_id, repo_id=101, ability="read", account_id=member_id
        )
    with pytest.raises(ValueError, match="always include write"):
        await set_repo_ability(
            db_session,
            tenant_id=tenant_id,
            repo_id=101,
            ability="read",
            account_id=admin_id,
        )
    live = await github_access.list_live_grant_repositories(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert len(live) == 1
    assert live[0][0].baseline_access == "write"
    assert live[0][0].ceiling_access == "write"
    await disconnect_repo(db_session, tenant_id=tenant_id, repo_id=101, account_id=admin_id)
    assert (
        await github_access.list_live_grant_repositories(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
        == []
    )
    assert (await connected_summary(db_session, tenant_id=tenant_id)).count == 1


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
            external_id="former-holder",
            account_id=member_id,
        )
    )
    await db_session.flush()
    with pytest.raises(ValueError, match="Only an admin"):
        await connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="former-holder",
            verified_tenant_admin=False,
        )
    account = await db_session.get(Account, member_id)
    assert account is not None
    assert account.role == "user"
    with pytest.raises(ValueError, match="platform admin"):
        await sync_connect_admin(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="former-holder",
            is_platform_admin=False,
        )
    account = await db_session.get(Account, member_id)
    assert account is not None
    assert account.role == "user"
    await sync_connect_admin(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="former-holder",
        is_platform_admin=True,
    )
    account = await db_session.get(Account, member_id)
    assert account is not None
    await db_session.refresh(account)
    assert account.role == "user", "GitHub helpers must never write role snapshots"
    with pytest.raises(ValueError, match="tenant admin"):
        await connect_link(
            db_session,
            settings=settings,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="former-holder",
            verified_tenant_admin=True,
        )
    with pytest.raises(ValueError, match="admin can connect GitHub"):
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
        origin_parent_channel_id="100",
        origin_thread_id="200",
        origin_followup_token="123:interaction-token",
        origin_followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    invitation = await get_invitation(db_session, digest(url.rsplit("/", 1)[-1]))
    assert invitation is not None
    assert invitation.requester_label == "clicker"
    assert (invitation.origin_parent_channel_id, invitation.origin_thread_id) == ("100", "200")
    assert invitation.encrypted_origin_followup is not None
    assert (
        decrypt_token(
            build_multifernet((key.get_secret_value(),)), invitation.encrypted_origin_followup
        )
        == "123:interaction-token"
    )
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
