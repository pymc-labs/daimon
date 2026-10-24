"""Connection invitation and requester access properties."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentGitHubGrant,
    CliPrincipal,
    GitHubConnectClickIntent,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    GitHubUserLink,
    PlatformPrincipal,
    Tenant,
    TenantAccessPolicyRecord,
    TenantGitHubRepo,
    ThreadSession,
)
from daimon.core.github_app_session import (
    close_headless_app_session,
)
from daimon.core.github_connect_delivery import poll_once
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.github_requester_access import (
    PermissionCache,
    effective_access,
    linked_permissions,
)
from daimon.core.stores import (
    github_access,
    github_app_installations,
    github_connect,
    github_issued_tokens,
    github_links,
)
from daimon.core.stores.github_connect_notices import ConnectNotice, claim_next, expire_old, settle
from daimon.core.stores.task_continuations import get_continuation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_connect_followup_resumes_task_once_and_keeps_repo_names_private(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="123"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_label="456",
        requester_platform_user_id="456",
        agent_id=agent_id,
        agent_name="ResearchBot",
        origin_platform="discord",
        origin_parent_channel_id="100",
        origin_thread_id="200",
        origin_ma_agent_id="ma-agent",
        origin_responder_name="CallingBot",
        requested_work="Review the issue",
        requested_repo="Team/Requested",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    assert invitation.requested_repo == "Team/Requested"
    row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert row is not None
    row.used_at = datetime.now(UTC)
    await db_session.flush()
    repo = github_connect.RepoConfirmation(
        repo_id=1, owner_id=1, installation_id=1, full_name="private/secret", max_access="write"
    )
    await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    wake_id = uuid.uuid5(uuid.NAMESPACE_URL, f"github-connect:{invitation.token_hash}")
    wake = await get_continuation(db_session, idempotency_key=wake_id)
    assert wake is not None
    assert wake.reason == "github_access_ready"
    assert wake.requested_work == "Review the issue"
    assert wake.thread_id == "200"
    assert wake.target_name == "CallingBot"
    assert "private/secret" not in wake.model_dump_json()
    assert await claim_next(db_session, platform="discord", now=datetime.now(UTC)) is None
    await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    assert await get_continuation(db_session, idempotency_key=wake_id) == wake


@pytest.mark.asyncio
async def test_connect_activation_grants_repos_from_two_installations(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ResearchBot",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    confirmed = [
        github_connect.RepoConfirmation(
            repo_id=repo_id,
            owner_id=installation_id,
            installation_id=installation_id,
            full_name=f"owner{installation_id}/repo{repo_id}",
            max_access="write",
        )
        for installation_id, repo_id in ((71, 101), (72, 102))
    ]
    for repo in confirmed:
        await github_app_installations.upsert(
            db_session,
            installation_id=repo.installation_id,
            account_login=f"owner{repo.installation_id}",
            repo_full_names=[repo.full_name],
        )
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo.repo_id,
                owner_id=repo.owner_id,
                installation_id=repo.installation_id,
                repo_full_name=repo.full_name,
                max_access="write",
                authorized_by_github_user_id=17,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    activated = await github_connect.activate_confirmed_agent(
        db_session, invitation=invitation, repos=confirmed
    )
    assert activated is not None
    assert {
        grant.repo_id
        for grant in await github_access.list_agent_grants(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
    } == {101, 102}


@pytest.mark.asyncio
async def test_teams_connect_followup_queues_the_task_wake_in_its_thread(
    db_session: AsyncSession,
) -> None:
    """Teams resumes interrupted work through the wake poller, like Discord and Slack."""
    tenant_id, admin_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="teams", external_id="entra-tenant"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_platform_user_id="aad-1",
        agent_id=agent_id,
        agent_name="ResearchBot",
        origin_platform="teams",
        origin_parent_channel_id="19:c@thread.tacv2",
        origin_thread_id="19:c@thread.tacv2;messageid=1",
        origin_ma_agent_id="ma-agent",
        origin_responder_name="CallingBot",
        requested_work="Review the issue",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    assert invitation.workspace_label == "this Teams organisation", "Teams is no Slack workspace"
    row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert row is not None
    row.used_at = datetime.now(UTC)
    await db_session.flush()
    repo = github_connect.RepoConfirmation(
        repo_id=1, owner_id=1, installation_id=1, full_name="private/secret", max_access="write"
    )
    await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    wake = await get_continuation(
        db_session,
        idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, f"github-connect:{invitation.token_hash}"),
    )
    assert wake is not None, "a Teams connect should queue the interrupted task"
    assert (wake.platform, wake.thread_id) == ("teams", "19:c@thread.tacv2;messageid=1"), (
        "the wake should run in the Teams thread that asked"
    )
    assert wake.available_at is not None, "the Teams wake poller only picks up due rows"


@pytest.mark.asyncio
async def test_bare_connect_notice_claim_is_single_and_failure_retries(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id="T1"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_label="U1",
        requester_platform_user_id="U1",
        origin_platform="slack",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    repo = github_connect.RepoConfirmation(
        repo_id=1, owner_id=1, installation_id=1, full_name="private/repo", max_access="read"
    )
    assert await claim_next(db_session, platform="slack", now=datetime.now(UTC)) is None
    row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert row is not None
    row.used_at = datetime.now(UTC)
    await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    first = await claim_next(db_session, platform="slack", now=datetime.now(UTC))
    assert first is not None and first.text == "Connected private/repo. Ready."
    assert await claim_next(db_session, platform="slack", now=datetime.now(UTC)) is None
    await settle(db_session, notice=first, delivered=False, now=datetime.now(UTC))
    assert await claim_next(db_session, platform="slack", now=datetime.now(UTC)) is None
    retry = await claim_next(
        db_session, platform="slack", now=datetime.now(UTC) + timedelta(minutes=3)
    )
    assert retry is not None
    await settle(db_session, notice=retry, delivered=True, now=datetime.now(UTC))
    assert await claim_next(db_session, platform="slack", now=datetime.now(UTC)) is None


@pytest.mark.asyncio
async def test_failed_notice_backs_off_while_another_delivers_and_expires(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id="T1"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    tokens = [
        await github_connect.mint_invitation(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=admin_id,
            requester_platform_user_id="U1",
            origin_platform="slack",
        )
        for _ in range(2)
    ]
    repo = github_connect.RepoConfirmation(
        repo_id=1, owner_id=1, installation_id=1, full_name="private/repo", max_access="read"
    )
    for index, token in enumerate(tokens):
        invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
        assert invitation is not None
        row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
        assert row is not None
        row.used_at = now - timedelta(seconds=2 - index)
        await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    first = await claim_next(db_session, platform="slack", now=now)
    assert first is not None and first.token_hash == github_connect.digest(tokens[0])
    await settle(db_session, notice=first, delivered=False, now=now)
    second = await claim_next(db_session, platform="slack", now=now)
    assert second is not None and second.token_hash == github_connect.digest(tokens[1])
    await settle(db_session, notice=second, delivered=True, now=now)
    assert await claim_next(db_session, platform="slack", now=now) is None
    assert (
        await claim_next(db_session, platform="slack", now=now + timedelta(minutes=3)) is not None
    )
    old = await db_session.get(GitHubConnectInvitation, github_connect.digest(tokens[0]))
    assert old is not None
    old.used_at = now - timedelta(hours=25)
    old.encrypted_origin_followup = b"spent"
    await expire_old(db_session, platform="slack", now=now)
    assert old.notice_delivered_at is not None
    assert old.encrypted_origin_followup is None


def test_connect_notice_mixed_access_label() -> None:
    notice = ConnectNotice(
        token_hash="a",
        tenant_id=uuid.uuid4(),
        requester_platform_user_id="U1",
        agent_name="ResearchBot",
        connected_repos=[
            {"name": "private/read", "access": "read"},
            {"name": "private/write", "access": "write"},
        ],
        notice_claimed_at=datetime.now(UTC),
    )
    assert notice.text == "Connected private/read, private/write. Ready."
    assert notice.public_text == "Connected 2 repo(s). Ready."


@pytest.mark.asyncio
async def test_requested_work_without_thread_queues_bare_notice(db_session: AsyncSession) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id="T1"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_platform_user_id="U1",
        origin_platform="slack",
        origin_ma_agent_id="caller",
        origin_responder_name="CallerBot",
        requested_work="  Review the open issue  ",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.requested_work == "Review the open issue"
    row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert row is not None
    row.used_at = datetime.now(UTC)
    repo = github_connect.RepoConfirmation(
        repo_id=1, owner_id=1, installation_id=1, full_name="private/repo", max_access="read"
    )
    await github_connect.queue_connect_followup(db_session, invitation=invitation, repos=[repo])
    assert row.requested_work is None
    assert await claim_next(db_session, platform="slack", now=datetime.now(UTC)) is not None
    invalid = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requested_work=" short ",
    )
    invalid_row = await github_connect.get_invitation(db_session, github_connect.digest(invalid))
    assert invalid_row is not None and invalid_row.requested_work is None


@pytest.mark.asyncio
async def test_discord_connect_button_reveals_only_to_requester_in_origin_thread(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="123"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(
        PlatformPrincipal(
            tenant_id=tenant_id,
            platform="discord",
            external_id="456",
            account_id=admin_id,
        )
    )
    await db_session.flush()
    intent_id = await github_connect.create_discord_connect_intent(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_platform_user_id="456",
        agent_id=agent_id,
        agent_name="ResearchBot",
        parent_channel_id="100",
        thread_id="200",
        origin_ma_agent_id="ma-agent",
        origin_responder_name="CallingBot",
        requested_work=None,
        requested_repo="Team/Requested",
    )
    stored_intent = await db_session.get(GitHubConnectClickIntent, intent_id)
    assert stored_intent is not None and stored_intent.requested_repo == "Team/Requested"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    args: dict[str, Any] = dict(
        intent_id=intent_id,
        tenant_id=tenant_id,
        fernet=fernet,
        encrypted_followup=b"encrypted-interaction",
        followup_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    assert (
        await github_connect.bind_discord_connect_click(
            db_session, requester_platform_user_id="999", thread_id="200", **args
        )
        is None
    )
    assert (
        await github_connect.bind_discord_connect_click(
            db_session, requester_platform_user_id="456", thread_id="201", **args
        )
        is None
    )
    first = await github_connect.bind_discord_connect_click(
        db_session, requester_platform_user_id="456", thread_id="200", **args
    )
    assert first is not None and first[1] == "ResearchBot"
    assert (
        await github_connect.bind_discord_connect_click(
            db_session, requester_platform_user_id="456", thread_id="200", **args
        )
        == first
    )
    intent = await db_session.get(GitHubConnectClickIntent, intent_id)
    assert intent is not None and intent.encrypted_token == first[0]
    token = github_connect.decrypt_token(fernet, first[0])
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    assert invitation.requested_repo == "Team/Requested"
    assert invitation.encrypted_origin_followup == b"encrypted-interaction"
    account = await db_session.get(Account, admin_id)
    assert account is not None
    account.role = "user"
    await db_session.flush()
    assert (
        await github_connect.bind_discord_connect_click(
            db_session, requester_platform_user_id="456", thread_id="200", **args
        )
        is None
    )
    account.role = "admin"
    await github_connect.create_flow(
        db_session,
        invitation_hash=invitation.token_hash,
        state="state",
        cookie="cookie",
        encrypted_verifier=b"verifier",
    )
    assert await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=12,
        repos=[],
    )
    assert await db_session.get(GitHubConnectClickIntent, intent_id) is None


@pytest.mark.asyncio
async def test_expired_click_intents_are_swept_without_a_new_click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, admin_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="123"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    intent_id = await github_connect.create_discord_connect_intent(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_platform_user_id="456",
        agent_id=agent_id,
        agent_name="Helper",
        parent_channel_id="100",
        thread_id="200",
        origin_ma_agent_id="ma-agent",
        origin_responder_name="Helper",
        requested_work=None,
    )
    row = await db_session.get(GitHubConnectClickIntent, intent_id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    await db_session.commit()
    delivered = AsyncMock(return_value=True)
    assert await poll_once(db_session_factory, platform="discord", deliver=delivered) == 0
    delivered.assert_not_awaited()
    async with db_session_factory() as session:
        assert await session.get(GitHubConnectClickIntent, intent_id) is None


def test_effective_access_properties() -> None:
    levels = ("none", "read", "write")
    for ability in levels:
        for asker in levels:
            result = effective_access({1: ability}, {1: asker}).get(1, "none")
            assert levels.index(result) == min(levels.index(ability), levels.index(asker))
    assert effective_access({1: "write"}, {}) == {}
    assert effective_access({1: "read"}, {1: "write"}) == {1: "read"}
    assert effective_access(
        {1: "write", 2: "write"}, {1: "none", 2: "read"}, {1: "read", 2: "none"}
    ) == {1: "read", 2: "read"}


def test_permission_cache_evicts_least_recently_used() -> None:
    cache = PermissionCache(max_entries=2)
    cache.put(1, 1, 1, {1: "read"})
    cache.put(1, 2, 1, {2: "read"})
    assert cache.get(1, 1, 1) == {1: "read"}
    cache.put(1, 3, 1, {3: "write"})
    assert cache.get(1, 2, 1) is None
    assert cache.get(1, 1, 1) == {1: "read"}


@pytest.mark.asyncio
async def test_pinned_agent_refuses_server_wide_repos(
    db_session: AsyncSession,
) -> None:
    """A pinned agent may use repos connected for it, never a server-wide one."""
    tenant_id, admin_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="client"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ClientBot",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    db_session.add(
        TenantAccessPolicyRecord(
            tenant_id=tenant_id,
            policy={"agent_rules": {"ClientBot": {"runs_in": ["client-channel"]}}},
        )
    )
    await db_session.flush()
    # With nothing granted yet, a link for the pinned agent is allowed.
    assert await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ClientBot",
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=5,
            owner_id=1,
            installation_id=1,
            repo_full_name="example/server-wide",
            max_access="read",
            authorized_by_github_user_id=1,
        )
    )
    await db_session.flush()
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=5,
            baseline_access="read",
            ceiling_access="read",
        )
    )
    await db_session.flush()
    with pytest.raises(
        github_connect.PinnedAgentConnectionError, match="only works in some channels"
    ):
        await github_connect.mint_invitation(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=admin_id,
            agent_id=agent_id,
            agent_name="ClientBot",
        )
    with pytest.raises(
        github_connect.PinnedAgentConnectionError, match="only works in some channels"
    ):
        await github_connect.activate_confirmed_agent(
            db_session,
            invitation=invitation,
            repos=[],
        )


@pytest.mark.asyncio
async def test_cancel_blocks_concurrent_github_confirmation(
    db_engine: AsyncEngine,
    db_clean: None,
) -> None:
    del db_clean
    sessionmaker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id="cancel-race"))
        await session.flush()
        session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
        await session.flush()
        invitation = await github_connect.mint_invitation(
            session, tenant_id=tenant_id, requester_account_id=admin_id
        )
        await github_connect.create_flow(
            session,
            invitation_hash=github_connect.digest(invitation),
            state="cancel-race-state",
            cookie="cancel-race-cookie",
            encrypted_verifier=b"encrypted",
        )
        assert await github_connect.set_user_token(
            session, state="cancel-race-state", encrypted_token=b"token", github_user_id=17
        )

    cancel_locked, release_cancel = asyncio.Event(), asyncio.Event()

    async def cancel() -> None:
        async with sessionmaker.begin() as session:
            flow = await github_connect.cancel_flow(
                session, state="cancel-race-state", cookie="cancel-race-cookie"
            )
            assert flow is not None and flow.cancelled_at is not None
            cancel_locked.set()
            await release_cancel.wait()

    async def confirm() -> bool:
        async with sessionmaker.begin() as session:
            return await github_connect.confirm(
                session,
                state="cancel-race-state",
                cookie="cancel-race-cookie",
                github_user_id=17,
                repos=[],
            )

    cancel_task = asyncio.create_task(cancel())
    await asyncio.wait_for(cancel_locked.wait(), timeout=5)
    confirm_task = asyncio.create_task(confirm())
    await asyncio.sleep(0.05)
    assert not confirm_task.done(), "confirmation must wait for cancellation's row lock"
    release_cancel.set()
    await cancel_task
    assert not await confirm_task


@pytest.mark.asyncio
async def test_expiry_sweep_rechecks_cancel_after_waiting_for_row_lock(
    db_engine: AsyncEngine, db_committing_engine: AsyncEngine, db_clean: None
) -> None:
    del db_clean
    sessionmaker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sessionmaker.begin() as session:
        session.add(
            Tenant(id=(tenant_id := uuid.uuid4()), platform="discord", external_id="sweep-race")
        )
        await session.flush()
        session.add(Account(id=(admin_id := uuid.uuid4()), tenant_id=tenant_id, role="admin"))
        await session.flush()
        invitation = await github_connect.mint_invitation(
            session, tenant_id=tenant_id, requester_account_id=admin_id
        )
        await github_connect.create_flow(
            session,
            invitation_hash=github_connect.digest(invitation),
            state="sweep-race-state",
            cookie="sweep-race-cookie",
            encrypted_verifier=b"encrypted",
        )
        assert await github_connect.set_user_token(
            session, state="sweep-race-state", encrypted_token=b"pending-token", github_user_id=17
        )
        flow = await session.get(GitHubConnectFlow, github_connect.digest("sweep-race-state"))
        assert flow is not None
        flow.expires_at = datetime.now(UTC) + timedelta(seconds=3)

    # The sweep starts against the old committed row while Cancel holds its
    # update lock. PostgreSQL must recheck the outer DELETE after Cancel commits.
    async with sessionmaker.begin() as cancel_session:
        cancelled = await github_connect.cancel_flow(
            cancel_session, state="sweep-race-state", cookie="sweep-race-cookie"
        )
        assert cancelled is not None and cancelled.cancelled_at is not None
        await asyncio.sleep(max(0, (flow.expires_at - datetime.now(UTC)).total_seconds() + 0.05))

        sweep_pid: int | None = None

        async def sweep() -> int:
            nonlocal sweep_pid
            async with sessionmaker.begin() as sweep_session:
                sweep_pid = await sweep_session.scalar(text("SELECT pg_backend_pid()"))
                assert sweep_pid is not None
                sweep_started.set()
                return await github_connect.delete_expired_flows(
                    sweep_session, now=datetime.now(UTC)
                )

        sweep_started = asyncio.Event()
        sweep_task = asyncio.create_task(sweep())
        try:
            await asyncio.wait_for(sweep_started.wait(), timeout=5)
            assert sweep_pid is not None
            # Confirm the DELETE reached the row lock before committing Cancel.
            async with AsyncSession(db_committing_engine) as probe:
                for _ in range(500):
                    waiting = await probe.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE pid = :pid AND wait_event_type = 'Lock' "
                            "AND query LIKE '%DELETE FROM github_connect_flows%')"
                        ),
                        {"pid": sweep_pid},
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("sweep did not wait for Cancel's row lock")
            assert not sweep_task.done()
        except BaseException:
            sweep_task.cancel()
            raise

    deleted = await asyncio.wait_for(sweep_task, timeout=5)
    assert deleted == 0
    async with sessionmaker() as session:
        retained = await session.get(GitHubConnectFlow, github_connect.digest("sweep-race-state"))
        assert retained is not None and retained.encrypted_user_token == b"pending-token"
        assert retained.cancelled_at is not None


@pytest.mark.asyncio
async def test_invitation_is_admin_only_and_single_use(db_session: AsyncSession) -> None:
    tenant_id, admin_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(Account(id=member_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=member_id
        )
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        requester_label="Alex",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.workspace_label == "this Discord server"
    assert invitation.requester_label == "Alex"
    assert invitation.expires_at > datetime.now(UTC) + timedelta(days=6)
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.get_flow(db_session, state="state", cookie="wrong") is None
    assert await github_connect.get_flow(db_session, state="state", cookie="cookie") is not None
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="other-browser",
        cookie="other-cookie",
        encrypted_verifier=b"encrypted",
    )
    saved = await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/repo",
                max_access="write",
            )
        ],
    )
    assert saved
    receipt = await github_connect.successful_confirmation(
        db_session, state="state", cookie="cookie"
    )
    assert receipt is not None and receipt.connected_repo_count == 1
    sibling = await db_session.get(GitHubConnectFlow, github_connect.digest("other-browser"))
    assert sibling is not None
    assert sibling.encrypted_verifier == b""
    assert sibling.encrypted_invitation_token is None
    assert sibling.encrypted_user_token is None
    assert sibling.expires_at > datetime.now(UTC) + timedelta(days=6)
    assert await github_connect.get_flow(db_session, state="state", cookie="cookie") is None
    assert await github_connect.get_invitation(db_session, github_connect.digest(token)) is None
    assert not await github_connect.confirm(
        db_session, state="state", cookie="cookie", github_user_id=17, repos=[]
    )
    expired_token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    expired = await db_session.get(GitHubConnectInvitation, github_connect.digest(expired_token))
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert (
        await github_connect.get_invitation(db_session, github_connect.digest(expired_token))
        is None
    )
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(expired_token),
        state="expired-flow",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    expired_flow = await db_session.get(GitHubConnectFlow, github_connect.digest("expired-flow"))
    assert expired_flow is not None
    expired_flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert await github_connect.get_flow(db_session, state="expired-flow", cookie="cookie") is None
    assert await github_connect.delete_expired_flows(db_session, now=datetime.now(UTC)) == 1
    assert await db_session.get(GitHubConnectFlow, github_connect.digest("expired-flow")) is None


@pytest.mark.asyncio
async def test_connect_link_requester_resolves_platform_admin_not_cli_operator(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    admin_id, cli_id, member_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
            Account(id=cli_id, tenant_id=tenant_id, role="user"),
            Account(id=member_id, tenant_id=tenant_id, role="user"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            CliPrincipal(tenant_id=tenant_id, os_user="operator", account_id=cli_id),
            PlatformPrincipal(
                tenant_id=tenant_id, platform="discord", external_id="123", account_id=admin_id
            ),
            PlatformPrincipal(
                tenant_id=tenant_id, platform="discord", external_id="456", account_id=member_id
            ),
            PlatformPrincipal(
                tenant_id=tenant_id,
                platform="slack",
                external_id="wrong-platform",
                account_id=admin_id,
            ),
        ]
    )
    await db_session.flush()

    assert (
        await github_connect.cli_account_id(db_session, tenant_id=tenant_id, os_user="operator")
        == cli_id
    )
    requester_id = await github_connect.admin_account_for_platform_user(
        db_session, tenant_id=tenant_id, external_id="123"
    )
    assert requester_id == admin_id
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=requester_id,
        requester_label="123",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.requester_account_id == admin_id

    for platform_user_id in ("456", "wrong-platform", "missing"):
        with pytest.raises(ValueError, match="workspace admin"):
            await github_connect.admin_account_for_platform_user(
                db_session, tenant_id=tenant_id, external_id=platform_user_id
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_status", "expected_access"),
    [("active", "write"), ("revoked", "write"), ("suspended", "write")],
)
async def test_reconfirmation_respects_inactive_repo_choice(
    db_session: AsyncSession, previous_status: str, expected_access: str
) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=55,
            installation_id=77,
            repo_full_name="example/repo",
            max_access="write",
            authorized_by_github_user_id=17,
            status=previous_status,
            version=1,
        )
    )
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/repo",
                max_access="write",
            )
        ],
    )
    repo = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert repo is not None
    assert repo.status == "active" and repo.max_access == expected_access
    assert repo.version == 2


async def _confirm_repo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    github_user_id: int,
    state: str,
    max_access: str,
    permission: str | None,
) -> bool:
    token = await github_connect.mint_invitation(
        session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    await github_connect.create_flow(
        session,
        invitation_hash=github_connect.digest(token),
        state=state,
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    return await github_connect.confirm(
        session,
        state=state,
        cookie="cookie",
        github_user_id=github_user_id,
        repos=[
            github_connect.RepoConfirmation.model_validate(
                {
                    "repo_id": 101,
                    "owner_id": 55,
                    "installation_id": 77,
                    "full_name": "example/repo",
                    "max_access": max_access,
                    "authorizer_permission": permission,
                }
            )
        ],
    )


@pytest.mark.asyncio
async def test_cross_agent_confirmations_serialize_before_agent_locks(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, admin_id, agent_a, agent_b = (uuid.uuid4() for _ in range(4))
    invitations: list[tuple[str, str, uuid.UUID, github_connect.RepoConfirmation]] = []
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
        await session.flush()
        for repo_id, name in ((101, "one/x"), (102, "two/y")):
            await github_app_installations.upsert(
                session,
                installation_id=repo_id,
                account_login=name.split("/", 1)[0],
                repo_full_names=[name],
            )
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=repo_id,
                    installation_id=repo_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=7,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_b,
            repo_id=101,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=admin_id,
        )
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_a,
            repo_id=102,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=admin_id,
        )
        for target, repo_id, name in ((agent_a, 101, "one/x"), (agent_b, 102, "two/y")):
            token = await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=admin_id,
                agent_id=target,
                agent_name="Helper",
            )
            state = str(uuid.uuid4())
            await github_connect.create_flow(
                session,
                invitation_hash=github_connect.digest(token),
                state=state,
                cookie="cookie",
                encrypted_verifier=b"encrypted",
            )
            invitations.append(
                (
                    state,
                    token,
                    target,
                    github_connect.RepoConfirmation(
                        repo_id=repo_id,
                        owner_id=repo_id,
                        installation_id=repo_id,
                        full_name=name,
                        max_access="write",
                    ),
                )
            )
    first_locked, release_first = asyncio.Event(), asyncio.Event()

    async def connect(
        state: str,
        token: str,
        target: uuid.UUID,
        repo: github_connect.RepoConfirmation,
        *,
        pause: bool,
    ) -> None:
        async with factory.begin() as session:
            invitation = await github_connect.get_invitation(session, github_connect.digest(token))
            assert invitation is not None
            if pause:
                await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
                first_locked.set()
                await release_first.wait()
            assert await github_connect.confirm(
                session, state=state, cookie="cookie", github_user_id=7, repos=[repo]
            )
            assert (
                await github_connect.activate_confirmed_agent(
                    session, invitation=invitation, repos=[repo], repo_locks_held=True
                )
                is not None
            )

    first = asyncio.create_task(connect(*invitations[0], pause=True))
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        second = asyncio.create_task(connect(*invitations[1], pause=False))
        await asyncio.sleep(0.05)
        assert not second.done()
    finally:
        release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    async with factory() as session:
        assert {
            g.repo_id
            for g in await github_access.list_agent_grants(
                session, tenant_id=tenant_id, agent_id=agent_a
            )
        } == {101, 102}
        assert {
            g.repo_id
            for g in await github_access.list_agent_grants(
                session, tenant_id=tenant_id, agent_id=agent_b
            )
        } == {101, 102}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installations", "read_only", "expected"),
    [
        (12, False, "would exceed 12 GitHub accounts or orgs"),
        (1, True, "would have read-only GitHub repos across accounts or orgs"),
        (11, False, None),
    ],
)
async def test_confirm_reassignment_checks_existing_agent_grants_before_writing(
    db_session: AsyncSession, installations: int, read_only: bool, expected: str | None
) -> None:
    tenant_id, admin_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    # The named agent invitation lets a server-wide move identify every grant holder.
    await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ResearchBot",
    )
    token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    state = f"transfer-{uuid.uuid4()}"
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state=state,
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    repo_ids = [101, 102] + [1000 + number for number in range(2, installations + 1)]
    for repo_id in repo_ids:
        installation_id = 1 if repo_id in (101, 102) else repo_id - 1000
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=installation_id,
                installation_id=installation_id,
                repo_full_name=f"owner{installation_id}/repo{repo_id}",
                max_access="write",
                authorized_by_github_user_id=7,
                status="active",
                version=1,
            )
        )
        db_session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access="read" if read_only else "write",
                ceiling_access="read" if read_only else "write",
            )
        )
    await db_session.flush()
    moved = github_connect.RepoConfirmation(
        repo_id=101,
        owner_id=13,
        installation_id=13,
        full_name="owner13/repo101",
        max_access="write",
    )
    also_confirmed = github_connect.RepoConfirmation(
        repo_id=102,
        owner_id=1,
        installation_id=1,
        full_name="owner1/repo102",
        max_access="write",
    )
    confirmed = [also_confirmed, moved]
    if expected is None:
        assert await github_connect.confirm(
            db_session,
            state=state,
            cookie="cookie",
            github_user_id=7,
            repos=confirmed,
        )
        assert (
            await github_access.repo_for_agent(
                db_session, tenant_id=tenant_id, repo_id=101, agent_id=agent_id
            )
        ).installation_id == 13
    else:
        with pytest.raises(github_connect.GrantReassignmentError, match=expected) as error:
            await github_connect.confirm(
                db_session,
                state=state,
                cookie="cookie",
                github_user_id=7,
                repos=confirmed,
            )
        assert "ResearchBot" in str(error.value)
        assert "owner13/repo101 moved to owner13" in str(error.value)
        assert "then reconnect" in str(error.value)
        unchanged = await github_access.repo_for_agent(
            db_session, tenant_id=tenant_id, repo_id=101, agent_id=agent_id
        )
        assert unchanged is not None and unchanged.installation_id == 1
        untouched = await github_access.repo_for_agent(
            db_session, tenant_id=tenant_id, repo_id=102, agent_id=agent_id
        )
        assert untouched is not None and untouched.version == 1
        invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
        assert invitation is not None and invitation.connected_repo_count is None


@pytest.mark.asyncio
async def test_confirm_records_the_authorizers_permission(db_session: AsyncSession) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    assert await _confirm_repo(
        db_session,
        tenant_id,
        admin_id,
        github_user_id=17,
        state="first",
        max_access="write",
        permission="maintain",
    )
    repo = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert repo is not None
    assert repo.authorized_permission == "maintain" and repo.max_access == "write"

    # A reader can authorize an App token with read and write access.
    assert await _confirm_repo(
        db_session,
        tenant_id,
        admin_id,
        github_user_id=99,
        state="second",
        max_access="read",
        permission="read",
    )
    repo = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert repo is not None
    assert repo.max_access == "write"
    assert (repo.authorized_by_github_user_id, repo.authorized_permission) == (99, "read")


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["read", "triage"])
async def test_confirm_allows_app_write_from_a_read_level_authorizer(
    db_session: AsyncSession, permission: str
) -> None:
    tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    assert await _confirm_repo(
        db_session,
        tenant_id,
        admin_id,
        github_user_id=17,
        state="confirmed",
        max_access="read",
        permission=permission,
    )
    repo = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert repo is not None
    assert repo.max_access == "write" and repo.authorized_permission == permission


@pytest.mark.asyncio
async def test_workspace_a_can_connect_org_repo_after_workspace_b(
    db_session: AsyncSession,
) -> None:
    await github_app_installations.upsert(
        db_session,
        installation_id=77,
        account_login="org-x",
        repo_full_names=["org-x/one", "org-x/two"],
    )
    connected: list[tuple[uuid.UUID, int]] = []
    for workspace, index, repo_name in (("B", 2, "org-x/two"), ("A", 1, "org-x/one")):
        tenant_id, admin_id = uuid.uuid4(), uuid.uuid4()
        db_session.add(Tenant(id=tenant_id, platform="discord", external_id=workspace))
        await db_session.flush()
        db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
        await db_session.flush()
        token = await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=admin_id
        )
        await github_connect.create_flow(
            db_session,
            invitation_hash=github_connect.digest(token),
            state=f"shared-state-{index}",
            cookie=f"shared-cookie-{index}",
            encrypted_verifier=b"encrypted",
        )
        assert await github_connect.confirm(
            db_session,
            state=f"shared-state-{index}",
            cookie=f"shared-cookie-{index}",
            github_user_id=17,
            repos=[
                github_connect.RepoConfirmation(
                    repo_id=index,
                    owner_id=55,
                    installation_id=77,
                    full_name=repo_name,
                    max_access="write",
                )
            ],
        )
        assert (
            await github_access.repo_for_agent(
                db_session, tenant_id=tenant_id, repo_id=index, agent_id=None
            )
            is not None
        )
        connected.append((tenant_id, index))
    assert all(
        [
            await github_access.repo_for_agent(
                db_session, tenant_id=tenant, repo_id=repo, agent_id=None
            )
            is not None
            for tenant, repo in connected
        ]
    )


@pytest.mark.asyncio
async def test_refresh_serializes_for_one_github_user(
    db_engine: AsyncEngine, db_committing_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    user_id = 98765
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=user_id,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes

        async def probe_row_lock() -> None:
            async with probe_sessionmaker.begin() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                assert await github_links.get_user_for_update(session, github_user_id=user_id)

        await asyncio.wait_for(probe_row_lock(), timeout=15)
        if request.url.path == "/login/oauth/access_token":
            refreshes += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "new",
                    "refresh_token": "refresh-new",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 123, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache(ttl_seconds=0)
        left, right = await asyncio.gather(
            *(
                linked_permissions(
                    sessionmaker,
                    client,
                    user_id=user_id,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
                for _ in range(2)
            )
        )
    assert left == right == {123: "read"}
    assert refreshes == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=user_id)
    assert row is not None and row.token_generation == 2 and row.status == "active"


@pytest.mark.asyncio
async def test_bad_refresh_token_does_not_break_a_rotated_link(
    db_engine: AsyncEngine, db_committing_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=681,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/login/oauth/access_token":
            refreshes += 1
            async with probe_sessionmaker.begin() as session:
                assert await github_links.rotate_user_tokens(
                    session,
                    github_user_id=681,
                    expected_generation=1,
                    encrypted_access_token=encrypt_token(fernet, "new"),
                    encrypted_refresh_token=encrypt_token(fernet, "refresh-new"),
                    access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            return httpx.Response(200, json={"error": "bad_refresh_token"})
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 101, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        permissions = await linked_permissions(
            sessionmaker,
            client,
            user_id=681,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert permissions == {101: "read"}
    assert refreshes == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=681)
    assert row is not None and row.status == "active"
    assert row.token_generation == 2 and row.link_generation == 1


@pytest.mark.asyncio
async def test_expired_refresh_does_not_break_a_rotated_link(
    db_engine: AsyncEngine,
    db_committing_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=682,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )

    original_bump = github_links.bump_link_generation
    raced = False

    async def bump_with_race(session: AsyncSession, **kwargs: object):
        nonlocal raced
        if not raced:
            raced = True
            async with probe_sessionmaker.begin() as probe:
                assert await github_links.rotate_user_tokens(
                    probe,
                    github_user_id=682,
                    expected_generation=1,
                    encrypted_access_token=encrypt_token(fernet, "new"),
                    encrypted_refresh_token=encrypt_token(fernet, "refresh-new"),
                    access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
        return await cast(Any, original_bump)(session, **kwargs)

    monkeypatch.setattr("daimon.core.github_requester_access.bump_link_generation", bump_with_race)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 101, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        permissions = await linked_permissions(
            sessionmaker,
            client,
            user_id=682,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert permissions == {101: "read"}
    assert raced
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=682)
    assert row is not None and row.status == "active"
    assert row.token_generation == 2 and row.link_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "broken"),
    [
        (200, {"error": "bad_refresh_token"}, True),
        (503, {"message": "unavailable"}, False),
        (429, {"message": "rate limited"}, False),
        (200, {"error": "incorrect_client_credentials"}, False),
    ],
)
async def test_refresh_only_breaks_invalid_grant(
    db_engine: AsyncEngine,
    db_clean: None,
    status: int,
    body: dict[str, str],
    broken: bool,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=678,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = linked_permissions(
            sessionmaker,
            client,
            user_id=678,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
        if broken:
            assert await call == {}
        elif body.get("error"):
            with pytest.raises(RuntimeError, match="incorrect_client_credentials"):
                await call
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await call
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=678)
    assert row is not None
    assert (row.status == "broken") is broken
    assert row.link_generation == (2 if broken else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_repo_permission_denial_is_cached_as_empty(
    db_engine: AsyncEngine, db_clean: None, status: int
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=679,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": "not available"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache()
        for _ in range(2):
            assert (
                await linked_permissions(
                    sessionmaker,
                    client,
                    user_id=679,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
                == {}
            )
    assert calls == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=679)
    assert row is not None and row.status == "active" and row.link_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"x-ratelimit-remaining": "0"}, "rate limited"),
        ({"retry-after": "60"}, "rate limited"),
        ({}, "You have exceeded a secondary rate limit."),
    ],
)
async def test_repo_permission_rate_limit_is_not_cached(
    db_engine: AsyncEngine, db_clean: None, headers: dict[str, str], message: str
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=680,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, headers=headers, json={"message": message})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache()
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await linked_permissions(
                    sessionmaker,
                    client,
                    user_id=680,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
    assert calls == 2


@pytest.mark.asyncio
async def test_user_token_401_invalidates_link(db_engine: AsyncEngine, db_clean: None) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=1234,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "token"),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unauthorized)) as client:
        result = await linked_permissions(
            sessionmaker,
            client,
            user_id=1234,
            installation_id=88,
            fernet=fernet,
            client_id="client",
            client_secret="secret",
            cache=PermissionCache(),
        )
    assert result == {}
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1234)
    assert row is not None and row.status == "broken" and row.link_generation == 2


@pytest.mark.asyncio
async def test_broken_link_is_distinct_from_no_link(db_session: AsyncSession) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
    db_session.add(
        GitHubUserLink(
            github_user_id=1234,
            login="alex",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="broken",
        )
    )
    await db_session.flush()
    assert not await github_links.account_link_is_broken(db_session, account_id=account_id)
    db_session.add(
        AccountGitHubLink(
            account_id=account_id,
            github_user_id=1234,
            platform="discord",
            platform_user_id="person",
            verified_via="discord_oauth",
        )
    )
    await db_session.flush()
    assert await github_links.account_link_is_broken(db_session, account_id=account_id)
    assert await github_links.account_link_status(db_session, account_id=account_id) is None


@pytest.mark.asyncio
async def test_unlink_bumps_generation_and_deletes_last_token(db_session: AsyncSession) -> None:
    tenant_id, account_a, account_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=account_a, tenant_id=tenant_id, role="user"))
    db_session.add(Account(id=account_b, tenant_id=tenant_id, role="user"))
    db_session.add(
        GitHubUserLink(
            github_user_id=1234,
            login="alex",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    for account_id in (account_a, account_b):
        db_session.add(
            AccountGitHubLink(
                account_id=account_id,
                github_user_id=1234,
                platform="discord",
                platform_user_id=str(account_id),
                verified_via="discord_oauth",
            )
        )
    await db_session.flush()
    assert await github_links.unlink_account(db_session, account_id=account_a) == 1234
    user = await github_links.get_user(db_session, github_user_id=1234)
    assert user is not None and user.link_generation == 2
    assert await github_links.unlink_account(db_session, account_id=account_b) == 1234
    assert await github_links.get_user(db_session, github_user_id=1234) is None


@pytest.mark.asyncio
async def test_headless_app_session_is_closed_only_after_runner_finishes(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="headless-test"))
    await db_session.flush()
    token = await github_issued_tokens.create_pending(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="headless-session",
        installation_id=909,
        repo_ids=[101],
        permissions={"contents": "read"},
        grant_versions={"grant:101": 1, "authorization:101": 1},
        expires_at=datetime.now(UTC) + timedelta(minutes=55),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await github_issued_tokens.store_token(
        db_session, token_id=token.token_id, token="headless-token", fernet=fernet
    )
    await github_issued_tokens.mark_delivered(db_session, token_id=token.token_id)
    started = datetime.now(UTC)
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="headless-session", except_ids=frozenset(), now=started
    )
    assert [
        row.token_id
        for row in await github_issued_tokens.select_due_superseded_tokens(db_session, now=started)
    ] == [token.token_id]
    await github_issued_tokens.restore_session_tokens(
        db_session, token_ids=frozenset({token.token_id})
    )
    assert (
        await github_issued_tokens.select_due_superseded_tokens(
            db_session, now=started + timedelta(minutes=10)
        )
        == []
    )
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id="headless-session", tenant_id=tenant_id, vault_id="session-vault"
    )
    await db_session.flush()

    now = datetime.now(UTC) + timedelta(minutes=2)
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=now) == []

    assert (
        await github_issued_tokens.finish_headless_app_session(
            db_session, session_id="headless-session"
        )
        == "session-vault"
    )
    closed = await github_issued_tokens.list_closed_app_sessions(db_session, now=now)
    assert [(row.session_id, row.vault_id) for row in closed] == [
        ("headless-session", "session-vault")
    ]
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="headless-session"
    )
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=now) == []


@pytest.mark.asyncio
async def test_headless_app_cleanup_archives_vault_without_grants(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="headless-zero"))
    await db_session.flush()
    await github_issued_tokens.register_headless_app_session(
        db_session,
        session_id="headless-zero-grants",
        tenant_id=tenant_id,
        vault_id="session-vault",
    )
    await db_session.commit()
    archive = AsyncMock()
    anthropic = SimpleNamespace(beta=SimpleNamespace(vaults=SimpleNamespace(archive=archive)))
    await close_headless_app_session(
        cast(Any, anthropic), db_session_factory, session_id="headless-zero-grants", fernet=None
    )
    archive.assert_awaited_once_with("session-vault")
    async with db_session_factory() as session:
        assert (
            await github_issued_tokens.list_closed_app_sessions(session, now=datetime.now(UTC))
            == []
        )


@pytest.mark.asyncio
async def test_mapped_app_vault_closes_without_issued_tokens(db_session: AsyncSession) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="mapped-zero"))
    await db_session.flush()
    await github_issued_tokens.register_app_session_vault(
        db_session,
        session_id="mapped-zero-grants",
        tenant_id=tenant_id,
        vault_id="mapped-vault",
    )
    mapping = ThreadSession(
        tenant_id=tenant_id,
        platform="discord",
        thread_id="mapped-zero-thread",
        ma_session_id="mapped-zero-grants",
        status="live",
    )
    db_session.add(mapping)
    await db_session.flush()
    assert (
        await github_issued_tokens.touch_unmapped_app_session(
            db_session, session_id="mapped-zero-grants"
        )
        is None
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC)) == []
    )
    mapping.status = "dead"
    await db_session.flush()
    closed = await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC))
    assert [(row.session_id, row.vault_id) for row in closed] == [
        ("mapped-zero-grants", "mapped-vault")
    ]
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="mapped-zero-grants"
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(db_session, now=datetime.now(UTC)) == []
    )


@pytest.mark.asyncio
async def test_unmapped_mcp_vault_survives_followups_until_the_turn_ceiling(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="mcp-unmapped"))
    await db_session.flush()
    await github_issued_tokens.register_app_session_vault(
        db_session,
        session_id="mcp-session",
        tenant_id=tenant_id,
        vault_id="mcp-vault",
        is_unmapped=True,
    )
    started = datetime.now(UTC)
    await db_session.execute(
        text(
            "UPDATE github_app_session_vaults SET last_started_at = :old "
            "WHERE session_id = 'mcp-session'"
        ),
        {"old": started - timedelta(minutes=40)},
    )
    assert await github_issued_tokens.list_closed_app_sessions(
        db_session, now=started + timedelta(minutes=7)
    )
    await github_issued_tokens.touch_unmapped_app_session(db_session, session_id="mcp-session")
    assert (
        await github_issued_tokens.list_closed_app_sessions(
            db_session, now=started + timedelta(minutes=7)
        )
        == []
    )
    assert (
        await github_issued_tokens.list_closed_app_sessions(
            db_session, now=started + timedelta(minutes=45)
        )
        == []
    )
    closed = await github_issued_tokens.list_closed_app_sessions(
        db_session, now=started + timedelta(minutes=47)
    )
    assert [(row.session_id, row.vault_id) for row in closed] == [("mcp-session", "mcp-vault")]
    assert (
        await github_issued_tokens.finish_headless_app_session(db_session, session_id="mcp-session")
        == "mcp-vault"
    )
    assert await github_issued_tokens.list_closed_app_sessions(db_session, now=started) == closed
    await github_issued_tokens.mark_headless_app_session_closed(
        db_session, session_id="mcp-session"
    )
    assert (
        await github_issued_tokens.touch_unmapped_app_session(db_session, session_id="mcp-session")
        is False
    )
