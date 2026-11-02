"""The purpose migration classifies existing agent credentials and round-trips."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_account
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0144_mcp_token_purpose.py"
    spec = importlib.util.spec_from_file_location("migration_mcp_token_purpose", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_backfill_classifies_existing_agent_rows(db_session: AsyncSession) -> None:
    """Report labels are report, day-or-less lifetimes internal, every other agent row coding."""
    account = await make_account(db_session)
    created = datetime(2026, 10, 1, tzinfo=UTC)
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(downgrade)
    rows: dict[str, tuple[str, str | None, timedelta | None, str | None]] = {
        # name: (kind, label, lifetime, surface)
        "coding": ("agent", "insight-bot", timedelta(days=90), None),
        "coding-no-expiry": ("agent", "old key", None, None),
        "report": ("agent", "report:q3-review", timedelta(days=30), None),
        "vault-chat": ("agent", "vault-chat:abc", timedelta(hours=1), None),
        "provider-turn": ("agent", "gemini-agent-turn", timedelta(days=60), "provider_turn"),
        "operator": ("operator", None, timedelta(days=30), None),
        "cli": ("cli", None, timedelta(days=30), None),
    }
    jtis: dict[str, uuid.UUID] = {}
    for name, (kind, label, lifetime, surface) in rows.items():
        jtis[name] = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO mcp_tokens (jti, account_id, tenant_id, agent_id, kind, label, "
                "created_at, expires_at, surface) VALUES (:jti, :account, :tenant, :agent, "
                ":kind, :label, :created, :expires, :surface)"
            ),
            {
                "jti": jtis[name],
                "account": account.id,
                "tenant": account.tenant_id,
                "agent": str(uuid.uuid4()) if kind == "agent" else None,
                "kind": kind,
                "label": label,
                "created": created,
                "expires": None if lifetime is None else created + lifetime,
                "surface": surface,
            },
        )

    await conn.run_sync(upgrade)

    found = {
        row.jti: row.purpose
        for row in await db_session.execute(text("SELECT jti, purpose FROM mcp_tokens"))
    }
    assert {name: found[jti] for name, jti in jtis.items()} == {
        "coding": "coding",
        "coding-no-expiry": "coding",
        "report": "report",
        "vault-chat": "internal",
        "provider-turn": "internal",
        "operator": None,
        "cli": None,
    }
