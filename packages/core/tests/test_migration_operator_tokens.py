"""The operator tokens migration round-trips; downgrading drops the agent-less tokens."""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.mcp_tokens import create_mcp_token_row
from daimon.testing.factories import make_account, make_mcp_token
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0035_operator_tokens.py"
    spec = importlib.util.spec_from_file_location("migration_operator_tokens", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_round_trip_keeps_agent_keys_and_drops_operator_tokens(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    agent_key = await make_mcp_token(db_session, account=account)
    await create_mcp_token_row(
        db_session,
        jti=uuid.uuid4(),
        account_id=account.id,
        tenant_id=account.tenant_id,
        agent_id=None,
        kind="operator",
        scopes={"tenant:read"},
        label=None,
        created_at=datetime.now(UTC),
    )
    migration = _migration()
    conn = await db_session.connection()

    def round_trip(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()
            migration.upgrade()

    await conn.run_sync(round_trip)

    rows = await db_session.execute(text("SELECT jti, kind, scopes, issued_usd FROM mcp_tokens"))
    assert [tuple(row) for row in rows] == [(agent_key.jti, "agent", [], 0)], (
        "agent keys survive as kind agent; a downgrade cannot keep agent-less tokens"
    )
