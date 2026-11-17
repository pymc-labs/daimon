"""Downgrading 0206 keeps only the rows the old ledger counted as one default credit."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_downgrade_when_plan_and_unsettled_rows_then_only_default_credits_stay(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    plan = (
        await db_session.execute(
            text(
                'INSERT INTO support_plans (tenant_id, platform, scope, scope_id, pool, "window")'
                " VALUES (:t, 'slack', 'workspace', '', 5, 'total') RETURNING id"
            ),
            {"t": tenant.id},
        )
    ).scalar_one()
    rows = {
        "default": ("default", None, "settled", 1),
        "plan": ("plan", plan, "settled", 1),
        "exempt": ("exempt", plan, "settled", 0),
        "declined": ("plan", plan, "declined", 0),
        "pending": ("plan", plan, "pending", None),
    }
    for message, (charged_to, limit_id, status, charge) in rows.items():
        await db_session.execute(
            text(
                "INSERT INTO support_escalations (tenant_id, platform, platform_user_id,"
                " channel_id, message_id, note, charged_to, limit_id, status, charge, hold)"
                " VALUES (:t, 'slack', 'U1', 'C1', :m, 'n', :c, :p, :s, :ch, 0)"
            ),
            {
                "t": tenant.id,
                "m": message,
                "c": charged_to,
                "p": limit_id,
                "s": status,
                "ch": charge,
            },
        )
    path = Path(__file__).parents[1] / "alembic/versions/0206_support_plans.py"
    spec = importlib.util.spec_from_file_location("migration_support_plans", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def downgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    await conn.run_sync(downgrade)
    left = (await db_session.execute(text("SELECT message_id FROM support_escalations"))).scalars()
    assert list(left) == ["default"], (
        "plan, exempt, declined and pending rows would be old default credits"
    )
