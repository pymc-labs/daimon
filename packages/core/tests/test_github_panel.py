"""Panel staging, PAT retirement and new-repo card claims."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core._models import (
    AgentFile,
    AgentGithubBinding,
    AgentRepoBinding,
    GitHubCredential,
    GitHubNewRepoNotice,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import Settings
from daimon.core.github_panel import (
    activate_grants,
    connect_link,
    load_grants_panel,
    stage_panel_grant,
)
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.github_connect import digest, get_invitation
from daimon.core.stores.github_new_repo_notices import (
    claim_next_notice,
    dismiss_notice,
    finish_notice,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _setup(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
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
async def test_panel_activation_retires_pat_and_keeps_new_drafts_separate(
    db_session: AsyncSession,
) -> None:
    tenant_id, agent_id = await _setup(db_session)
    with pytest.raises(ValueError, match="Stage write access"):
        await activate_grants(db_session, tenant_id=tenant_id, agent_id=agent_id, account_id=None)
    assert await db_session.get(GitHubCredential, agent_id) is not None
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
    before = await load_grants_panel(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert before.mode == "legacy" and before.repos[0].staged
    assert await activate_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id, account_id=None
    )
    assert await db_session.get(GitHubCredential, agent_id) is None
    assert await db_session.get(AgentGithubBinding, agent_id) is None
    assert (
        await db_session.scalars(select(AgentFile).where(AgentFile.agent_id == agent_id))
    ).all() == []
    assert (
        await github_access.get_agent_mode(db_session, tenant_id=tenant_id, agent_id=agent_id)
        == "app"
    )
    await stage_panel_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=102,
        baseline_access="read",
        ceiling_access="read",
        account_id=None,
        is_working_repo=False,
    )
    pending = await load_grants_panel(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert pending.has_pending and pending.repos[1].staged
    assert pending.repos[1].live_baseline is None
    assert (
        len(
            await github_access.list_live_grant_repositories(
                db_session, tenant_id=tenant_id, agent_id=agent_id
            )
        )
        == 1
    )
    assert not await activate_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id, account_id=None
    )
    live = await load_grants_panel(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert not live.has_pending and live.repos[1].live_baseline == "read"


@pytest.mark.asyncio
async def test_new_repo_card_claim_release_and_one_delivery(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    now = datetime.now(UTC)
    db_session.add(
        GitHubNewRepoNotice(
            tenant_id=tenant_id,
            installation_id=999,
            repo_full_name="example/new",
            queued_at=now,
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
async def test_connection_invitation_names_clicker_and_preselects_notice(
    db_session: AsyncSession,
) -> None:
    tenant_id, _ = await _setup(db_session)
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
            crypto=SimpleNamespace(keys=["key"]),
        ),
    )
    url = await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="clicker",
        preselected_repo_full_name="example/new",
    )
    invitation = await get_invitation(db_session, digest(url.rsplit("/", 1)[-1]))
    assert invitation is not None
    assert invitation.requester_label == "clicker"
    assert invitation.preselected_repo_full_name == "example/new"
