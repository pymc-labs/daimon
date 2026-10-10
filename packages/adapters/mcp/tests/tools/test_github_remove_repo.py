"""Conversational removal is scoped to the agent and the requester's later yes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_remove_repo as tool
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import effective_repo_url_sets
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.session_compat import (
    DEFAULT_MA_CAPABILITIES,
    ReplaceSession,
    decide_session_compatibility,
)
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores import agent_repo_binding, github_access, github_app_installations
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.github_grant_proposals import resolve
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_remove_repo_requires_later_same_requester_yes_and_is_agent_scoped(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"remove-{uuid.uuid4().hex[:8]}")
        admin = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        channel_admin = await make_account(session, tenant=tenant)
        await set_role(session, admin.id, Role.ADMIN)
        await set_role(session, other.id, Role.ADMIN)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="channel",
            role_ids=[],
            user_ids=["channel-admin"],
            actor_account_id=None,
        )
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"Agent": {"runs_in": ["channel"]}}}
            ),
        )
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_agent")
        other_agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_other")
        await github_app_installations.upsert(
            session, installation_id=98765, account_login="owner", repo_full_names=["owner/repo"]
        )
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id) VALUES "
                "(:tenant, 12345, :agent, 12, 98765, 'owner/repo', 'read', 17, :account), "
                "(:tenant, 12345, :other, 12, 98765, 'owner/repo', 'read', 17, :account)"
            ),
            {"tenant": tenant.id, "agent": agent_id, "other": other_agent_id, "account": admin.id},
        )
        for target in (agent_id, other_agent_id):
            await github_access.stage_grant(
                session,
                tenant_id=tenant.id,
                agent_id=target,
                repo_id=12345,
                baseline_access="read",
                ceiling_access="read",
                granted_by_account_id=admin.id,
            )
            await github_access.activate_agent(session, tenant_id=tenant.id, agent_id=target)
        await github_access.set_working_repo(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_name="owner/repo",
            account_id=admin.id,
        )
        await agent_repo_binding.set_binding(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url="owner/repo",
            default_branch="main",
            ma_secret_ref="legacy",
            proof=None,
        )
        await session.execute(
            text(
                "INSERT INTO agent_github_grant_drafts "
                "(tenant_id, agent_id, repo_id, operation) "
                "VALUES (:tenant, :agent, 12345, 'upsert')"
            ),
            {"tenant": tenant.id, "agent": agent_id},
        )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC) - timedelta(seconds=1),
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="channel",
        thread_id="thread",
    )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    auth = AuthIdentity(
        account_id=admin.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="admin",
        is_admin=True,
    )

    async def call(who: AuthIdentity = auth, *, confirmed: bool = False) -> tool.RemoveRepoResult:
        return await tool.remove_repo_impl(
            runtime,  # type: ignore[arg-type]
            who,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/repo",
            confirmed=confirmed,
        )

    with pytest.raises(ToolError, match="Ask a server admin"):
        await call(replace(auth, account_id=member.id, role=Role.USER, is_admin=False))
    channel_auth = replace(
        auth,
        account_id=channel_admin.id,
        role=Role.USER,
        platform_user_id="channel-admin",
        is_admin=False,
    )
    assert (await call(channel_auth)).status == "proposed"
    agent.metadata["daimon_managed"] = "true"
    with pytest.raises(ToolError, match="Ask a server admin"):
        await call(channel_auth)
    agent.metadata.clear()
    proposal = await call()
    assert proposal.message == "Remove owner/repo from Agent?"
    assert (await call(confirmed=True)).status == "proposed"  # same turn
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    assert (
        await call(replace(auth, account_id=other.id, platform_user_id="other"), confirmed=True)
    ).status == "proposed"
    assert (await call(confirmed=True)).status == "proposed"  # no human yes
    async with committing_sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_grant_proposals SET expires_at = now() - interval '1 second' "
                "WHERE requester_account_id = :account"
            ),
            {"account": admin.id},
        )
    assert (await call(confirmed=True)).status == "proposed"  # expired
    await call()  # a fresh proposal
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=2)
    async with committing_sessionmaker.begin() as session:
        await resolve(
            session,
            origin=SimpleNamespace(
                tenant_id=tenant.id,
                account_id=admin.id,
                platform="discord",
                thread_id="thread",
                id=origin.id,
                created_at=origin.created_at,
            ),  # type: ignore[arg-type]
            message_text="yes",
        )
    removed = await call(confirmed=True)
    assert removed.status == "removed"
    desired_token_urls, desired_mounted_urls = await effective_repo_url_sets(
        committing_sessionmaker,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=admin.id,
        is_external=False,
        config=GithubAppSettings(),
        fernet=None,
    )
    assert (desired_token_urls, desired_mounted_urls) == ((), ())
    recorded = SessionSnapshot(
        ma_agent_id="ma_agent",
        model_id="claude-sonnet-5",
        system_sha256="system",
        skills_sha256="skills",
        environment_id="environment",
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        token_repo_urls=("https://github.com/owner/repo",),
        repo_urls=("https://github.com/owner/repo",),
        memory_store_id=None,
        vault_id=None,
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
        agent_version=1,
        agent_name="Agent",
    )
    desired = recorded.model_copy(
        update={"token_repo_urls": desired_token_urls, "repo_urls": desired_mounted_urls}
    )
    assert decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=datetime.now(UTC),
    ) == ReplaceSession(reasons=("repo_set",))
    with pytest.raises(ToolError, match="not on this agent"):
        await call(confirmed=True)
    async with committing_sessionmaker() as session:
        assert (
            await github_access.list_agent_grants(session, tenant_id=tenant.id, agent_id=agent_id)
            == []
        )
        assert (
            len(
                await github_access.list_agent_grants(
                    session, tenant_id=tenant.id, agent_id=other_agent_id
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM agent_github_grant_drafts "
                    "WHERE tenant_id = :tenant AND agent_id = :agent AND repo_id = 12345"
                ),
                {"tenant": tenant.id, "agent": agent_id},
            )
            == 0
        )
        assert (
            await agent_repo_binding.get_binding(session, tenant_id=tenant.id, agent_id=agent_id)
            is None
        )
        own = await github_access.repo_for_agent(
            session, tenant_id=tenant.id, repo_id=12345, agent_id=agent_id
        )
        other_repo = await github_access.repo_for_agent(
            session, tenant_id=tenant.id, repo_id=12345, agent_id=other_agent_id
        )
        assert own is not None and own.status == "revoked"
        assert other_repo is not None and other_repo.status == "active"


@pytest.mark.asyncio
async def test_remove_repo_refuses_channel_admin_without_live_rights_or_managed_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"remove-rights-{uuid.uuid4().hex[:8]}")
        account = await make_account(session, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="other-channel",
            role_ids=[],
            user_ids=["channel-admin"],
            actor_account_id=None,
        )
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"Agent": {"runs_in": ["channel"]}}}
            ),
        )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        deployment_default=DeploymentDefault(),
        group_lookups=None,
    )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="other-channel",
        thread_id="thread",
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="channel-admin",
    )
    with pytest.raises(ToolError, match="Ask a server admin"):
        await tool.remove_repo_impl(
            runtime,  # type: ignore[arg-type]
            auth,
            origin_context_id=str(uuid.uuid4()),
            repo_name="owner/repo",
        )
