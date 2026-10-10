"""Requested repo columns can be removed and restored with existing links."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Account, GitHubConnectClickIntent, GitHubConnectInvitation, Tenant
from daimon.core.stores import github_connect
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_requested_repo_migration_round_trips(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0081_github_connect_requested_repo.py"
    spec = importlib.util.spec_from_file_location("migration_github_connect_requested_repo", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def has_column(table: str) -> bool:
        return bool(
            await db_session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table AND column_name = 'requested_repo')"
                ),
                {"table": table},
            )
        )

    tenant_id, account_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="123"))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requested_repo="team/one",
    )
    intent_id = await github_connect.create_discord_connect_intent(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_platform_user_id="456",
        agent_id=agent_id,
        agent_name="Bot",
        parent_channel_id="100",
        thread_id="200",
        origin_ma_agent_id="ma-agent",
        origin_responder_name="Bot",
        requested_work=None,
        requested_repo="team/two",
    )
    assert await has_column("github_connect_invitations")
    assert await has_column("github_connect_click_intents")
    await conn.run_sync(run("downgrade"))
    assert not await has_column("github_connect_invitations")
    assert not await has_column("github_connect_click_intents")
    assert await db_session.scalar(text("SELECT count(*) FROM github_connect_invitations")) == 1
    assert await db_session.scalar(text("SELECT count(*) FROM github_connect_click_intents")) == 1
    await conn.run_sync(run("upgrade"))
    assert await has_column("github_connect_invitations")
    assert await has_column("github_connect_click_intents")
    db_session.expire_all()
    invitation = await db_session.get(GitHubConnectInvitation, github_connect.digest(token))
    intent = await db_session.get(GitHubConnectClickIntent, intent_id)
    assert invitation is not None and invitation.requested_repo is None
    assert intent is not None and intent.requested_repo is None
