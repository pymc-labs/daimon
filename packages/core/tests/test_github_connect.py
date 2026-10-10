"""Connection invitation and requester access properties."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.core import github_app_session
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentFile,
    AgentGithubBinding,
    AgentGitHubGrant,
    AgentGitHubMode,
    AgentRepoBinding,
    AgentSkillRepoCredential,
    CliPrincipal,
    GitHubConnectClickIntent,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    GitHubConnectRequest,
    GitHubIssuedToken,
    GitHubUserLink,
    PlatformPrincipal,
    Tenant,
    TenantAccessPolicyRecord,
    TenantGitHubRepo,
    ThreadSession,
)
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    AppSessionAccess,
    AppToken,
    archive_app_vault,
    close_headless_app_session,
)
from daimon.core.github_connect_delivery import claim_next, poll_once, settle
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.github_requester_access import (
    PermissionCache,
    effective_access,
    linked_permissions,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import (
    agent_files,
    github_access,
    github_app_installations,
    github_connect,
    github_issued_tokens,
    github_links,
)
from daimon.core.stores.github_connect_notices import ConnectNotice, expire_old
from daimon.core.stores.task_continuations import get_continuation
from sqlalchemy import func, select, text
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
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None
    row = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert row is not None
    row.used_at = datetime.now(UTC)
    row.activation_status = "activated"
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
    assert first is not None and first.text == "Connected private/repo, Read only. Ready."
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
    assert notice.text == "Connected private/read, private/write, Mixed access. Ready."
    assert notice.public_text == "Connected 2 repo(s), Mixed access. Ready."


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
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    args = dict(
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


@pytest.mark.asyncio
async def test_headless_app_run_uses_baseline_not_ceiling(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="headless-baseline"))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session,
        installation_id=99001,
        account_login="example",
        repo_full_names=["example/baseline"],
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=99002,
            owner_id=1,
            installation_id=99001,
            repo_full_name="example/baseline",
            max_access="write",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    db_session.add(AgentGitHubMode(tenant_id=tenant_id, agent_id=agent_id, mode="app"))
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=99002,
            baseline_access="read",
            ceiling_access="write",
            staged=False,
            is_working_repo=False,
            version=1,
        )
    )
    await db_session.commit()
    async with httpx.AsyncClient() as client:
        rows, _, _ = await github_app_session._effective_rows(  # pyright: ignore[reportPrivateUsage]
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            config=GithubAppSettings(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
        )
    assert [access for _, _, access in rows] == ["read"]


def test_permission_cache_evicts_least_recently_used() -> None:
    cache = PermissionCache(max_entries=2)
    cache.put(1, 1, 1, {1: "read"})
    cache.put(1, 2, 1, {2: "read"})
    assert cache.get(1, 1, 1) == {1: "read"}
    cache.put(1, 3, 1, {3: "write"})
    assert cache.get(1, 2, 1) is None
    assert cache.get(1, 1, 1) == {1: "read"}


@pytest.mark.asyncio
async def test_client_pinned_agent_refuses_server_wide_repos(
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
    with pytest.raises(github_connect.ClientAgentConnectionError, match="saved GitHub key"):
        await github_connect.mint_invitation(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=admin_id,
            agent_id=agent_id,
            agent_name="ClientBot",
        )
    with pytest.raises(github_connect.ClientAgentConnectionError, match="saved GitHub key"):
        await github_connect.activate_confirmed_agent(
            db_session,
            invitation=invitation,
            repos=[],
        )
    pending = await db_session.get(GitHubConnectInvitation, invitation.token_hash)
    assert pending is not None
    pending.used_at = datetime.now(UTC)
    pending.activation_status = "update_pending"
    await db_session.flush()
    assert not await github_connect.activate_pending_agent(
        db_session, tenant_id=tenant_id, agent_id=agent_id, account_id=admin_id
    )


@pytest.mark.parametrize(
    "saved_state", ["binding", "gh_token", "github_token", "working", "skill", "runs_in"]
)
@pytest.mark.asyncio
async def test_agent_link_allows_saved_github_state_at_mint(
    db_session: AsyncSession, saved_state: str
) -> None:
    tenant_id, admin_id, active_agent, mint_agent = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=active_agent,
        agent_name="FreshThenSaved",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and not invitation.operator_issued
    if saved_state == "runs_in":
        db_session.add(
            TenantAccessPolicyRecord(
                tenant_id=tenant_id,
                policy={
                    "agent_rules": {
                        "FreshThenSaved": {"runs_in": ["client-channel"]},
                        "AlreadySaved": {"runs_in": ["client-channel"]},
                    }
                },
            )
        )
    else:
        for agent_id in (active_agent, mint_agent):
            if saved_state == "binding":
                db_session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
            elif saved_state in ("gh_token", "github_token"):
                db_session.add(
                    AgentFile(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        key="GH_TOKEN" if saved_state == "gh_token" else "GITHUB_TOKEN",
                        content="encrypted-placeholder",
                        encoding="plain",
                    )
                )
            elif saved_state == "working":
                db_session.add(
                    AgentRepoBinding(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_url="example/work",
                        default_branch="main",
                        ma_secret_ref="saved-key",
                    )
                )
            else:
                db_session.add(
                    AgentSkillRepoCredential(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_url="example/skill",
                        default_branch="main",
                        ma_secret_ref="saved-key",
                        proof_kind="private",
                    )
                )
    await db_session.flush()
    # Whoever manages the agent may start switching it; confirming decides
    # whether the saved key goes (see the update_pending tests).
    assert await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=mint_agent,
        agent_name="AlreadySaved",
    )
    with pytest.raises(ValueError, match="No authorized repositories remain"):
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
    db_engine: AsyncEngine, db_nullpool_engine: AsyncEngine, db_clean: None
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
            async with AsyncSession(db_nullpool_engine) as probe:
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
async def test_mint_waits_for_concurrent_agent_key_write(
    db_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del db_clean
    committing_sessionmaker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    tenant_id, admin_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
        await session.flush()
        session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    held, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_lock = agent_files.lock_agent_keys

    async def observed_lock(
        session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> None:
        if session.info.get("minter"):
            attempted.set()
        await original_lock(session, tenant_id=tenant_id, agent_id=agent_id)

    monkeypatch.setattr(agent_files, "lock_agent_keys", observed_lock)

    async def write_key() -> None:
        async with committing_sessionmaker.begin() as session:
            await agent_files.lock_agent_keys(session, tenant_id=tenant_id, agent_id=agent_id)
            session.add(
                AgentFile(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    key="GH_TOKEN",
                    content="encrypted-placeholder",
                    encoding="plain",
                )
            )
            await session.flush()
            held.set()
            await release.wait()

    async def mint() -> None:
        async with committing_sessionmaker.begin() as session:
            session.info["minter"] = True
            await github_connect.mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=admin_id,
                agent_id=agent_id,
                agent_name="ResearchBot",
            )

    writer = asyncio.create_task(write_key())
    try:
        await asyncio.wait_for(held.wait(), timeout=10)
        minter = asyncio.create_task(mint())
        await asyncio.wait_for(attempted.wait(), timeout=10)
        release.set()
        await writer
        # A link for one agent may switch a saved key, so the mint goes ahead,
        # but only after the concurrent key write committed.
        await minter
    finally:
        release.set()
        if not writer.done():
            await writer


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
    external_id = uuid.uuid4()
    db_session.add(Account(id=external_id, tenant_id=tenant_id, role="admin", is_external=True))
    await db_session.flush()
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=external_id
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
    admin = await db_session.get(Account, admin_id)
    assert admin is not None
    admin.is_external = True
    await db_session.flush()
    assert await github_connect.get_invitation(db_session, github_connect.digest(token)) is None
    admin.is_external = False
    await db_session.flush()
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
                max_access="read",
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
async def test_agent_bound_connection_activates_without_a_saved_key_and_waits_with_one(
    db_session: AsyncSession,
) -> None:
    tenant_id, admin_id, member_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
            Account(id=member_id, tenant_id=tenant_id, role="user"),
        ]
    )
    await db_session.flush()
    confirmation = github_connect.RepoConfirmation(
        repo_id=101,
        owner_id=55,
        installation_id=77,
        full_name="example/work",
        max_access="write",
    )
    for index, has_key in enumerate((False, True)):
        ma_agent_id = f"ma-agent-{index}"
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        await github_connect.record_connect_request(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=member_id,
            agent_id=agent_id,
            agent_name="ResearchBot",
        )
        await github_connect.record_connect_request(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=member_id,
            agent_id=agent_id,
            agent_name="ResearchBot",
        )
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(GitHubConnectRequest)
                .where(GitHubConnectRequest.agent_id == agent_id)
            )
            == 1
        )
        token = await github_connect.mint_invitation(
            db_session,
            tenant_id=tenant_id,
            requester_account_id=admin_id,
            agent_id=agent_id,
            agent_name="ResearchBot",
            operator_issued=has_key,
        )
        invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
        assert invitation is not None and invitation.agent_id == agent_id
        if has_key:
            db_session.add(AgentGithubBinding(agent_id=agent_id, principal_id=agent_id))
            db_session.add(
                AgentFile(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    key="GH_TOKEN",
                    content="encrypted-placeholder",
                    encoding="plain",
                )
            )
            db_session.add(
                ThreadSession(
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id="old-chat",
                    ma_session_id="old-session",
                    ma_agent_id=ma_agent_id,
                    status="live",
                )
            )
            await db_session.flush()
        await github_connect.create_flow(
            db_session,
            invitation_hash=github_connect.digest(token),
            state=f"state-{index}",
            cookie=f"cookie-{index}",
            encrypted_verifier=b"encrypted",
        )
        assert await github_connect.confirm(
            db_session,
            state=f"state-{index}",
            cookie=f"cookie-{index}",
            github_user_id=17,
            repos=[confirmation],
        )
        await github_app_installations.upsert_github_app(
            db_session,
            installation_id=77,
            account_id=55,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        status = await github_connect.activate_confirmed_agent(
            db_session, invitation=invitation, repos=[confirmation]
        )
        grants = await github_access.list_agent_grants(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        )
        assert [(row.baseline_access, row.ceiling_access, row.staged) for row in grants] == [
            ("write", "write", has_key)
        ]
        assert status is not None
        assert status.status == ("update_pending" if has_key else "activated")
        assert await github_access.get_agent_mode(
            db_session, tenant_id=tenant_id, agent_id=agent_id
        ) == ("legacy" if has_key else "app")
        if not has_key:
            await github_access.stage_grant(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=101,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=admin_id,
                mount_path="/workspace/work",
                is_working_repo=True,
            )
            db_session.add(
                AgentRepoBinding(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_url="example/old-work",
                    default_branch="main",
                    ma_secret_ref="leftover-key",
                )
            )
            db_session.add(
                AgentSkillRepoCredential(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_url="example/old-skill",
                    default_branch="main",
                    ma_secret_ref="leftover-key",
                    proof_kind="private",
                )
            )
            await db_session.flush()
            # App-mode agents remain manageable even when old local bindings exist.
            assert await github_connect.mint_invitation(
                db_session,
                tenant_id=tenant_id,
                requester_account_id=admin_id,
                agent_id=agent_id,
                agent_name="ResearchBot",
            )
        if has_key:
            assert (
                await github_connect.pending_update_for_agent(
                    db_session, tenant_id=tenant_id, agent_id=agent_id
                )
                is not None
            )
            assert await github_connect.activate_pending_agent(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=admin_id,
            )
            assert await db_session.get(AgentGithubBinding, agent_id) is None
            assert await db_session.get(AgentFile, (tenant_id, agent_id, "GH_TOKEN")) is None
            session_row = await db_session.scalar(
                select(ThreadSession).where(ThreadSession.ma_agent_id == ma_agent_id)
            )
            assert session_row is not None and session_row.fresh_start_requested_at is not None
            assert (
                await github_access.get_agent_mode(
                    db_session, tenant_id=tenant_id, agent_id=agent_id
                )
                == "app"
            )
            assert not await github_connect.activate_pending_agent(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=admin_id,
            )


@pytest.mark.parametrize("legacy_kind", ["working", "skill"])
@pytest.mark.asyncio
async def test_repo_credential_agents_wait_for_explicit_update(
    db_session: AsyncSession, legacy_kind: str
) -> None:
    tenant_id, admin_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    if legacy_kind == "working":
        db_session.add(
            AgentRepoBinding(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="example/work",
                default_branch="main",
                ma_secret_ref="saved-key",
            )
        )
    else:
        db_session.add(
            AgentSkillRepoCredential(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="example/work",
                default_branch="main",
                ma_secret_ref="saved-key",
                proof_kind="private",
            )
        )
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        agent_id=agent_id,
        agent_name="ResearchBot",
        operator_issued=True,
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.operator_issued
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    confirmation = github_connect.RepoConfirmation(
        repo_id=101,
        owner_id=55,
        installation_id=77,
        full_name="example/work",
        max_access="write",
    )
    assert await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[confirmation],
    )
    await github_app_installations.upsert_github_app(
        db_session,
        installation_id=77,
        account_id=55,
        account_login="example",
        account_type="Organization",
        repository_selection="selected",
        suspended_at=None,
    )
    pending = await github_connect.activate_confirmed_agent(
        db_session, invitation=invitation, repos=[confirmation]
    )
    assert pending is not None and pending.status == "update_pending"
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "legacy"
    )


@pytest.mark.asyncio
async def test_connect_link_requester_resolves_platform_admin_not_cli_operator(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    admin_id, cli_id, member_id, external_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
            Account(id=cli_id, tenant_id=tenant_id, role="user"),
            Account(id=member_id, tenant_id=tenant_id, role="user"),
            Account(id=external_id, tenant_id=tenant_id, role="admin", is_external=True),
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
                tenant_id=tenant_id, platform="discord", external_id="789", account_id=external_id
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

    for platform_user_id in ("456", "789", "wrong-platform", "missing"):
        with pytest.raises(ValueError, match="workspace admin"):
            await github_connect.admin_account_for_platform_user(
                db_session, tenant_id=tenant_id, external_id=platform_user_id
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_status", "expected_access"),
    [("active", "write"), ("revoked", "read"), ("suspended", "read")],
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
                max_access="read",
            )
        ],
    )
    repo = await github_access.repo_for_agent(
        db_session, tenant_id=tenant_id, repo_id=101, agent_id=None
    )
    assert repo is not None
    assert repo.status == "active" and repo.max_access == expected_access
    assert repo.version == 2


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
    db_engine: AsyncEngine, db_nullpool_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
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
    db_engine: AsyncEngine, db_nullpool_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
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
    db_nullpool_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    probe_sessionmaker = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
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
        return await original_bump(session, **kwargs)

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
async def test_issued_tokens_stale_after_bump_unlink_and_relink(db_session: AsyncSession) -> None:
    tenant_id, account_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
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
            authorized_by_github_user_id=501,
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
    db_session.add(
        GitHubUserLink(
            github_user_id=501,
            login="first",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    db_session.add(
        AccountGitHubLink(
            account_id=account_id,
            github_user_id=501,
            platform="discord",
            platform_user_id="person",
            verified_via="discord_oauth",
        )
    )
    await db_session.flush()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    async def issued(generation: int) -> uuid.UUID:
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=f"session-{generation}",
            installation_id=909,
            repo_ids=[101],
            permissions={"contents": "read"},
            grant_versions={"grant:101": 1, "authorization:101": 1},
            requester_account_id=account_id,
            github_user_id=501,
            link_generation=generation,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token="issued", fernet=fernet
        )
        return row.token_id

    first = await issued(1)
    assert await github_issued_tokens.select_stale_tokens(db_session) == []
    await github_links.bump_link_generation(db_session, github_user_id=501)
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first
    }
    second = await issued(2)
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first
    }
    assert await github_links.unlink_account(db_session, account_id=account_id) == 501
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first,
        second,
    }

    db_session.add(
        GitHubUserLink(
            github_user_id=777,
            login="second",
            encrypted_access_token=b"encrypted",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    db_session.add(
        AccountGitHubLink(
            account_id=account_id,
            github_user_id=777,
            platform="discord",
            platform_user_id="person",
            verified_via="discord_oauth",
        )
    )
    await db_session.flush()
    assert {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)} == {
        first,
        second,
    }


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
        anthropic, db_session_factory, session_id="headless-zero-grants", fernet=None
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


@pytest.mark.asyncio
async def test_app_vault_archive_accepts_already_archived() -> None:
    response = httpx.Response(
        404,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/vaults/archived/archive"),
    )
    archive = AsyncMock(
        side_effect=APIStatusError("Vault already archived", response=response, body=None)
    )
    anthropic = SimpleNamespace(beta=SimpleNamespace(vaults=SimpleNamespace(archive=archive)))
    await archive_app_vault(anthropic, vault_id="archived")
    archive.assert_awaited_once_with("archived")


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse_resource", [False, True])
async def test_live_app_rotation_updates_ma_and_delays_revocation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    refuse_resource: bool,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(AgentGitHubMode(tenant_id=tenant_id, agent_id=agent_id, mode="app"))
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    old_ids: list[uuid.UUID] = []
    for installation_id, repo_id, name in ((101, 11, "first"), (102, 12, "second")):
        await github_app_installations.upsert(
            db_session,
            installation_id=installation_id,
            account_login="acme",
            repo_full_names=[f"acme/{name}"],
        )
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                owner_id=1,
                installation_id=installation_id,
                repo_full_name=f"acme/{name}",
                max_access="read",
                authorized_by_github_user_id=501,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    for _, repo_id, name in ((101, 11, "first"), (102, 12, "second")):
        db_session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access="read",
                ceiling_access="read",
                staged=False,
                is_working_repo=name == "first",
                version=1,
            )
        )
    await db_session.flush()
    for installation_id, repo_id in ((101, 11), (102, 12)):
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="rotating-session",
            installation_id=installation_id,
            repo_ids=[repo_id],
            permissions={"contents": "read"},
            grant_versions={f"grant:{repo_id}": 1, f"authorization:{repo_id}": 1},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        old_ids.append(row.token_id)
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token=f"old-{repo_id}", fernet=fernet
        )
        await github_issued_tokens.mark_delivered(db_session, token_id=row.token_id)
    await db_session.commit()

    monkeypatch.setattr(github_app_session, "build_app_jwt", lambda *_args, **_kwargs: "jwt")
    mint = AsyncMock(side_effect=["new-first", "new-second"])
    monkeypatch.setattr(github_app_session, "mint_installation_token", mint)
    writes: list[tuple[str, str, dict[str, object]]] = []
    credentials = [
        {"id": f"cred-{name}", "type": "credential", "vault_id": "vault-1", "auth": auth}
        for name, auth in (
            ("first", {"type": "environment_variable", "secret_name": "GH_TOKEN_ACME_READ"}),
            ("second", {"type": "environment_variable", "secret_name": "GH_TOKEN_ACME_READ_1"}),
            ("working", {"type": "environment_variable", "secret_name": "GH_TOKEN"}),
            (
                "copilot",
                {
                    "type": "static_bearer",
                    "mcp_server_url": "https://api.githubcopilot.com/mcp",
                },
            ),
        )
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"data": credentials, "has_more": False})
        writes.append((req.method, req.url.path, json.loads(req.content) if req.content else {}))
        if req.method == "DELETE":
            return httpx.Response(204)
        if "/resources/" in req.url.path:
            if refuse_resource:
                return httpx.Response(409, json={"error": {"message": "turn is running"}})
            return httpx.Response(
                200,
                json={
                    "id": req.url.path.rsplit("/", 1)[-1],
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "mount_path": "/workspace/acme/first",
                    "type": "github_repository",
                    "url": "https://github.com/acme/first",
                },
            )
        return httpx.Response(
            200,
            json=next(item for item in credentials if req.url.path.endswith(item["id"])),
        )

    anthropic = AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await github_app_session.rotate_live_app_tokens(
        anthropic,
        db_session_factory,
        session_id="rotating-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault-1",
        resource_ids={
            "https://github.com/acme/first": "resource-first",
            "https://github.com/acme/second": "resource-second",
        },
        config=GithubAppSettings(app_id="1", private_key="dummy"),
        fernet=fernet,
        active_turn=True,
    )
    assert mint.await_count == 2
    # Only the working repo is mounted; the other repo's token lives in the vault.
    assert {path for _, path, _ in writes if "/resources/" in path} == {
        "/v1/sessions/rotating-session/resources/resource-first",
    }
    assert {path for _, path, _ in writes if "/credentials/" in path} == {
        f"/v1/vaults/vault-1/credentials/cred-{name}"
        for name in ("first", "second", "working", "copilot")
    }
    async with db_session_factory() as session:
        issued = await github_issued_tokens.list_session_tokens(
            session, session_id="rotating-session"
        )
        old = [row for row in issued if row.token_id in old_ids]
        assert all(row.superseded_at is not None for row in old)
        assert all(row.revoke_after == row.expires_at + timedelta(minutes=6) for row in old)
        assert (
            len([row for row in issued if row.status == "delivered" and row.superseded_at is None])
            == 2
        )
        assert (
            await github_issued_tokens.select_due_superseded_tokens(
                session, now=max(row.revoke_after for row in old) - timedelta(seconds=1)
            )
            == []
        )
        assert {
            row.token_id
            for row in await github_issued_tokens.select_due_superseded_tokens(
                session, now=max(row.revoke_after for row in old)
            )
        } == set(old_ids)
    current = await github_app_session._current_app_access(  # pyright: ignore[reportPrivateUsage]
        db_session_factory,
        session_id="rotating-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    assert [token.credential_name for token in current.tokens] == [
        "GH_TOKEN_ACME_READ",
        "GH_TOKEN_ACME_READ_1",
    ]
    from daimon.adapters.scheduler.main import (
        _sweep_github_app_tokens,  # pyright: ignore[reportPrivateUsage]
    )

    revoked: list[str] = []

    async def fake_revoke(_client: httpx.AsyncClient, token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr("daimon.adapters.scheduler.main.revoke_token", fake_revoke)
    await _sweep_github_app_tokens(db_session_factory, fernet=fernet)
    assert revoked == []
    async with db_session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE github_issued_tokens SET expires_at = :expired, revoke_after = :due "
                "WHERE token_id = ANY(:old_ids)"
            ),
            {
                "expired": datetime.now(UTC) - timedelta(minutes=10),
                "due": datetime.now(UTC) - timedelta(seconds=1),
                "old_ids": old_ids,
            },
        )
    await _sweep_github_app_tokens(db_session_factory, fernet=fernet)
    assert revoked == []
    async with db_session_factory() as session:
        issued = await github_issued_tokens.list_session_tokens(
            session, session_id="rotating-session"
        )
        assert all(row.status == "revoked" for row in issued if row.token_id in old_ids)
    async with db_session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE tenant_github_repos SET status = 'revoked' "
                "WHERE tenant_id = :tenant_id AND repo_id = 12"
            ),
            {"tenant_id": tenant_id},
        )
    current = await github_app_session._current_app_access(  # pyright: ignore[reportPrivateUsage]
        db_session_factory,
        session_id="rotating-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    assert len(current.resources) == 2
    mint.reset_mock(side_effect=True)
    mint.side_effect = ["narrowed-first"]
    writes.clear()
    await github_app_session.rotate_live_app_tokens(
        anthropic,
        db_session_factory,
        session_id="rotating-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault-1",
        resource_ids={
            "https://github.com/acme/first": "resource-first",
            "https://github.com/acme/second": "resource-second",
        },
        config=GithubAppSettings(app_id="1", private_key="dummy"),
        fernet=fernet,
        active_turn=True,
    )
    mint.assert_awaited_once()
    assert {path for _, path, _ in writes if "/resources/" in path} == {
        "/v1/sessions/rotating-session/resources/resource-first"
    }
    assert ("DELETE", "/v1/vaults/vault-1/credentials/cred-second") in {
        (method, path) for method, path, _ in writes
    }
    await _sweep_github_app_tokens(db_session_factory, fernet=fernet)
    assert revoked == ["new-second"]


async def test_superseded_token_survives_version_bumps_but_not_hard_changes(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session, installation_id=101, account_login="acme", repo_full_names=["acme/first"]
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=11,
            owner_id=1,
            installation_id=101,
            repo_full_name="acme/first",
            max_access="write",
            authorized_by_github_user_id=501,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=11,
            baseline_access="read",
            ceiling_access="read",
            staged=False,
            is_working_repo=False,
            version=1,
        )
    )
    await db_session.flush()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    now = datetime.now(UTC)
    token_ids: dict[str, uuid.UUID] = {}
    for label, contents in (("old", "read"), ("live", "read")):
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="long-turn",
            installation_id=101,
            repo_ids=[11],
            permissions={"contents": contents},
            grant_versions={"grant:11": 1, "authorization:11": 1},
            expires_at=now + timedelta(minutes=50),
        )
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token=f"{label}-token", fernet=fernet
        )
        await github_issued_tokens.mark_delivered(db_session, token_id=row.token_id)
        token_ids[label] = row.token_id
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="long-turn", except_ids=frozenset({token_ids["live"]}), now=now
    )

    async def stale() -> set[uuid.UUID]:
        return {row.token_id for row in await github_issued_tokens.select_stale_tokens(db_session)}

    # A ceiling raise and a working-repo move mid-turn bump both versions.
    await db_session.execute(
        text(
            "UPDATE agent_github_grants SET ceiling_access = 'write', is_working_repo = true, "
            "version = 3 WHERE tenant_id = :tenant_id"
        ),
        {"tenant_id": tenant_id},
    )
    await db_session.execute(
        text("UPDATE tenant_github_repos SET version = 2 WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    assert await stale() == {token_ids["live"]}

    await db_session.execute(
        text("UPDATE agent_github_grants SET staged = true WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    assert await stale() == set(token_ids.values())
    await db_session.execute(
        text("UPDATE agent_github_grants SET staged = false WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    await db_session.execute(
        text("UPDATE tenant_github_repos SET status = 'revoked' WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    assert await stale() == set(token_ids.values())
    await db_session.execute(
        text("UPDATE tenant_github_repos SET status = 'active' WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    await db_session.execute(
        text(
            'UPDATE github_issued_tokens SET permissions = \'{"contents": "write"}\' '
            "WHERE token_id = :token_id"
        ),
        {"token_id": token_ids["old"]},
    )
    assert await stale() == {token_ids["live"]}
    await db_session.execute(
        text("UPDATE agent_github_grants SET ceiling_access = 'read' WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    assert await stale() == set(token_ids.values())


async def test_later_narrowing_revokes_an_earlier_superseded_write_token_now(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    start = datetime.now(UTC)

    async def deliver(label: str, contents: str) -> uuid.UUID:
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="narrowing",
            installation_id=101,
            repo_ids=[11],
            permissions={"contents": contents},
            grant_versions={"grant:11": 1, "authorization:11": 1},
            expires_at=start + timedelta(minutes=50),
        )
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token=label, fernet=fernet
        )
        await github_issued_tokens.mark_delivered(db_session, token_id=row.token_id)
        return row.token_id

    first = await deliver("first", "write")
    # Rotation at equal access: the first write token is left to expire.
    second = await deliver("second", "write")
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="narrowing", except_ids=frozenset({second}), now=start
    )
    later = start + timedelta(minutes=20)
    assert await github_issued_tokens.select_due_superseded_tokens(db_session, now=later) == []
    # The baseline drops to read (ceiling still write): the next replacement is
    # read-only, so both earlier write tokens are revoked now.
    third = await deliver("third", "read")
    await github_issued_tokens.mark_session_tokens_superseded(
        db_session, session_id="narrowing", except_ids=frozenset({third}), now=later
    )
    due = await github_issued_tokens.select_due_superseded_tokens(db_session, now=later)
    assert {row.token_id for row in due} == {first, second}


async def test_active_turn_refresh_proceeds_without_a_rollback_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Prepared(Exception):
        pass

    monkeypatch.setattr(
        github_app_session,
        "_current_app_access",
        AsyncMock(side_effect=ValueError("current App token cannot be restored")),
    )
    monkeypatch.setattr(github_app_session, "prepare_app_access", AsyncMock(side_effect=Prepared))
    with pytest.raises(Prepared):
        await github_app_session.rotate_live_app_tokens(
            AsyncMock(),
            AsyncMock(),
            session_id="no-snapshot",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            account_id=None,
            is_external=False,
            vault_id="vault-1",
            resource_ids={},
            config=GithubAppSettings(app_id="1", private_key="dummy"),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            active_turn=True,
        )


async def test_app_rotation_keeps_old_session_when_first_resource_update_fails(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    access = AppSessionAccess(
        resources=(
            {
                "type": "github_repository",
                "url": "https://github.com/owner/repo",
                "authorization_token": "new-token",
            },
        ),
        tokens=(),
        working_token=None,
    )
    monkeypatch.setattr(github_app_session, "prepare_app_access", AsyncMock(return_value=access))
    revoke = AsyncMock()
    monkeypatch.setattr(github_app_session, "revoke_app_access", revoke)
    update = AsyncMock(side_effect=RuntimeError("resource update failed"))
    archive = AsyncMock()
    anthropic = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(resources=SimpleNamespace(update=update), archive=archive)
        )
    )
    with pytest.raises(RuntimeError, match="resource update failed"):
        await github_app_session.rotate_live_app_tokens(
            anthropic,
            db_session_factory,
            session_id="existing-session",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            account_id=None,
            is_external=True,
            vault_id="existing-vault",
            resource_ids={"https://github.com/owner/repo": "resource-1"},
            config=GithubAppSettings(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        )
    archive.assert_not_awaited()
    revoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("narrowing", [False, True])
async def test_active_turn_failure_keeps_delivered_tokens_unless_wider(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    narrowing: bool,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    expires_at = datetime.now(UTC) + timedelta(minutes=55)

    async def issue(session_id: str, label: str, contents: str) -> AppToken:
        row = await github_issued_tokens.create_pending(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            installation_id=101,
            repo_ids=[11],
            permissions={"contents": contents},
            grant_versions={"grant:11": 1, "authorization:11": 1},
            expires_at=expires_at,
        )
        await github_issued_tokens.store_token(
            db_session, token_id=row.token_id, token=label, fernet=fernet
        )
        return AppToken(row.token_id, label, 101, (11,), contents, f"GH_TOKEN_{label.upper()}")

    old = await issue("running-session", "old", "read")
    await github_issued_tokens.mark_delivered(db_session, token_id=old.token_id)
    await db_session.commit()
    url = "https://github.com/acme/repo"
    new_contents = "write" if narrowing else "read"

    async def prepare(*_args: object, provisional_session_id: str, **_kwargs: object):
        mounted = await issue(provisional_session_id, "mounted", new_contents)
        undelivered = await issue(provisional_session_id, "undelivered", new_contents)
        await db_session.commit()
        return AppSessionAccess(
            resources=(
                {
                    "type": "github_repository",
                    "url": url,
                    "authorization_token": "mounted",
                    "mount_path": "/workspace/acme/repo",
                },
            ),
            tokens=(mounted, undelivered),
            working_token="mounted",
        )

    async def credentials_fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("vault update failed")

    monkeypatch.setattr(
        github_app_session,
        "_current_app_access",
        AsyncMock(side_effect=ValueError("current App token cannot be restored")),
    )
    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare)
    monkeypatch.setattr(github_app_session, "add_app_credentials", credentials_fail)
    revoked: list[str] = []

    async def fake_revoke(_client: httpx.AsyncClient, token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(github_app_session, "revoke_token", fake_revoke)
    archive = AsyncMock()
    anthropic = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(resources=SimpleNamespace(update=AsyncMock()), archive=archive)
        )
    )
    with pytest.raises(RuntimeError, match="vault update failed"):
        await github_app_session.rotate_live_app_tokens(
            anthropic,
            db_session_factory,
            session_id="running-session",
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            is_external=False,
            vault_id="running-vault",
            resource_ids={url: "resource-1"},
            config=GithubAppSettings(app_id="1", private_key="dummy"),
            fernet=fernet,
            active_turn=True,
        )
    archive.assert_not_awaited()
    async with db_session_factory() as session:
        rows = {
            row.token_id: row
            for row in await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.tenant_id == tenant_id)
            )
        }
    by_label = {
        github_issued_tokens.decrypt_issued_token(
            github_issued_tokens.IssuedToken.model_validate(row), fernet=fernet
        ): row
        for row in rows.values()
    }
    assert by_label["old"].status == "delivered"
    assert by_label["old"].superseded_at is None
    assert by_label["undelivered"].status == "revoked"
    mounted = by_label["mounted"]
    if narrowing:
        # Wider than the read token the session still holds: revoked now.
        assert sorted(revoked) == ["mounted", "undelivered"]
        assert mounted.status == "revoked"
    else:
        # Only the token MA never received is revoked; the mounted one expires.
        assert revoked == ["undelivered"]
        assert mounted.session_id == "running-session"
        assert mounted.status == "delivered"
        assert mounted.superseded_at is not None
        assert mounted.revoke_after == mounted.expires_at + timedelta(minutes=6)


async def test_active_turn_failure_after_swap_without_snapshot_is_not_archived(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://github.com/owner/repo"
    new = AppSessionAccess(
        resources=(
            {
                "type": "github_repository",
                "url": url,
                "authorization_token": "new-token",
                "mount_path": "/workspace/owner/repo",
            },
        ),
        tokens=(AppToken(uuid.uuid4(), "new-token", 1, (11,), "read", "GH_TOKEN_OWNER_READ"),),
        working_token="new-token",
    )
    monkeypatch.setattr(
        github_app_session,
        "_current_app_access",
        AsyncMock(side_effect=ValueError("current App token cannot be restored")),
    )
    monkeypatch.setattr(github_app_session, "prepare_app_access", AsyncMock(return_value=new))
    monkeypatch.setattr(
        github_app_session,
        "add_app_credentials",
        AsyncMock(side_effect=RuntimeError("vault update failed")),
    )
    monkeypatch.setattr(github_app_session, "revoke_app_access", AsyncMock())
    update = AsyncMock()
    archive = AsyncMock()
    anthropic = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(resources=SimpleNamespace(update=update), archive=archive)
        )
    )
    with pytest.raises(RuntimeError, match="vault update failed"):
        await github_app_session.rotate_live_app_tokens(
            anthropic,
            db_session_factory,
            session_id="running-session",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            account_id=None,
            is_external=True,
            vault_id="running-vault",
            resource_ids={url: "resource-1"},
            config=GithubAppSettings(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            active_turn=True,
        )
    update.assert_awaited_once()
    archive.assert_not_awaited()


async def test_active_turn_rotation_failure_restores_old_credentials_without_archiving(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://github.com/owner/repo"
    old = AppSessionAccess(
        resources=(
            {
                "type": "github_repository",
                "url": url,
                "authorization_token": "old-token",
                "mount_path": "/workspace/owner/repo",
            },
        ),
        tokens=(AppToken(uuid.uuid4(), "old-token", 1, (11,), "read", "GH_TOKEN_OWNER_READ"),),
        working_token="old-token",
    )
    new = AppSessionAccess(
        resources=(
            {
                "type": "github_repository",
                "url": url,
                "authorization_token": "new-token",
                "mount_path": "/workspace/owner/repo",
            },
        ),
        tokens=(AppToken(uuid.uuid4(), "new-token", 1, (11,), "read", "GH_TOKEN_OWNER_READ"),),
        working_token="new-token",
    )
    monkeypatch.setattr(github_app_session, "_current_app_access", AsyncMock(return_value=old))
    monkeypatch.setattr(github_app_session, "prepare_app_access", AsyncMock(return_value=new))
    credentials = AsyncMock(side_effect=[RuntimeError("vault update failed"), None])
    monkeypatch.setattr(github_app_session, "add_app_credentials", credentials)
    revoke = AsyncMock()
    monkeypatch.setattr(github_app_session, "revoke_app_access", revoke)
    supersede = AsyncMock()
    monkeypatch.setattr(github_app_session, "mark_session_tokens_superseded", supersede)
    update = AsyncMock()
    archive = AsyncMock()
    anthropic = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(resources=SimpleNamespace(update=update), archive=archive)
        )
    )
    with pytest.raises(RuntimeError, match="vault update failed"):
        await github_app_session.rotate_live_app_tokens(
            anthropic,
            db_session_factory,
            session_id="running-session",
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            account_id=None,
            is_external=True,
            vault_id="running-vault",
            resource_ids={url: "resource-1"},
            config=GithubAppSettings(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            active_turn=True,
        )
    assert [call.kwargs["authorization_token"] for call in update.await_args_list] == [
        "new-token",
        "old-token",
    ]
    assert [call.kwargs["access"] for call in credentials.await_args_list] == [new, old]
    revoke.assert_awaited_once()
    assert revoke.await_args.args[2] == new
    supersede.assert_not_awaited()
    archive.assert_not_awaited()
