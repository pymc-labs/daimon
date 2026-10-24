"""Push resync uses the agent's current GitHub App grant."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core.config import GithubAppSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync import resync as resync_module
from daimon.core.skill_sync.orchestrator import SyncReport
from daimon.core.skill_sync.resync import resync_bound_repo, should_resync
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_cli_principal
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_resync_only_matches_the_imported_branch() -> None:
    assert should_resync("refs/heads/main", "main")
    assert should_resync("refs/heads/develop", "develop")
    assert not should_resync("refs/tags/v1", "main")
    assert not should_resync("refs/heads/main", "develop")


async def test_resync_runs_only_after_an_active_app_grant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = await make_cli_principal(db_session, os_user="app-resync")
    repo = "example/widgets"
    await upsert_user_skill(
        db_session,
        tenant_id=cli.tenant_id,
        principal_id=cli.id,
        agent_name="agent-a",
        name="sample",
        source_repo_url=f"https://github.com/{repo}",
        source_repo_branch="main",
        source_path="",
        content_hash="hash",
        anthropic_id="skill_sample",
        anthropic_latest_version="1",
    )
    handler = make_fake_ma_handler()
    client = build_fake_anthropic(handler)
    agent = await client.beta.agents.create(
        name="agent-a",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent-a"},
    )
    agent_id = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id=agent.id)
    await github_app_installations.upsert(
        db_session, installation_id=9001, account_login="example", repo_full_names=[repo]
    )
    await db_session.execute(
        text(
            "INSERT INTO tenant_github_repos "
            "(tenant_id, repo_id, owner_id, installation_id, repo_full_name, max_access, "
            "authorized_by_github_user_id) "
            "VALUES (:tenant, 1001, 17, 9001, :repo, 'read', 17)"
        ),
        {"tenant": cli.tenant_id, "repo": repo},
    )
    await db_session.commit()
    sync = AsyncMock(return_value=SyncReport(synced=1))
    monkeypatch.setattr(resync_module, "sync_agent_skills", sync)
    app = GithubAppSettings(app_id="123", private_key=SecretStr("test-key"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(599))
    ) as http:
        result = await resync_bound_repo(
            repo_full_name=repo,
            ref="refs/heads/main",
            sessionmaker=db_session_factory,
            fernet=make_fernet(),
            anthropic_client=client,
            http_client=http,
            github_app=app,
        )
        assert result.failed_bindings == 0
        sync.assert_not_awaited()
        async with db_session_factory.begin() as session:
            await github_access.stage_grant(
                session,
                tenant_id=cli.tenant_id,
                agent_id=agent_id,
                repo_id=1001,
                baseline_access="read",
                ceiling_access="read",
                granted_by_account_id=None,
            )
        result = await resync_bound_repo(
            repo_full_name=repo,
            ref="refs/heads/main",
            sessionmaker=db_session_factory,
            fernet=make_fernet(),
            anthropic_client=client,
            http_client=http,
            github_app=app,
        )
    assert result.failed_bindings == 0
    sync.assert_awaited_once()
    assert sync.await_args.kwargs["app_id"] == "123"
    assert sync.await_args.kwargs["target_ma_agent_id"] == agent.id
