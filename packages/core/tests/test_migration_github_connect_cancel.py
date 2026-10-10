"""Downgrade must preserve unfinished GitHub token revocations."""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Account, GitHubConnectFlow, Tenant
from daimon.core.stores import github_connect
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0077_github_connect_cancel.py"
    spec = importlib.util.spec_from_file_location("migration_github_connect_cancel", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_downgrade_refuses_pending_cancelled_token(db_session: AsyncSession) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="migration-cancel"))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    invitation = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=account_id
    )
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(invitation),
        state="migration-cancel-state",
        cookie="migration-cancel-cookie",
        encrypted_verifier=b"verifier",
    )
    flow = await db_session.get(GitHubConnectFlow, github_connect.digest("migration-cancel-state"))
    assert flow is not None
    flow.encrypted_user_token = b"pending-revocation"
    flow.cancelled_at = datetime.now(UTC)
    await db_session.flush()

    migration = _migration()
    conn = await db_session.connection()

    def run(sync_conn: Connection, step: str) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    with pytest.raises(RuntimeError, match="tokens pending revocation"):
        await conn.run_sync(lambda sync_conn: run(sync_conn, "downgrade"))
    assert (
        await db_session.scalar(
            text("SELECT encrypted_user_token FROM github_connect_flows WHERE state_hash = :state"),
            {"state": flow.state_hash},
        )
        == b"pending-revocation"
    )
    assert (
        await db_session.scalar(
            text("SELECT cancelled_at FROM github_connect_flows WHERE state_hash = :state"),
            {"state": flow.state_hash},
        )
        is not None
    )

    flow.encrypted_user_token = None
    await db_session.flush()
    await conn.run_sync(lambda sync_conn: run(sync_conn, "downgrade"))
    assert not await db_session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'github_connect_flows' "
            "AND column_name = 'cancelled_at')"
        )
    )
    await conn.run_sync(lambda sync_conn: run(sync_conn, "upgrade"))
