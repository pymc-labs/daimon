"""The channel budgets migration round-trips and backfills routine channels."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0033_channel_budgets.py"
    spec = importlib.util.spec_from_file_location("migration_channel_budgets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_downgrade_then_upgrade_backfills_known_routine_channels(
    db_session: AsyncSession,
) -> None:
    slack = await make_tenant(db_session, platform="slack")
    discord = await make_tenant(db_session, platform="discord")
    for tenant, kind, destination in [
        (discord, "channel", "111"),
        (discord, "thread", "222"),
        (slack, "thread", "C1:1717.5"),
        (slack, None, None),
    ]:
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id="u",
            agent_id="agent",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=str(destination),
            destination_kind=kind,  # type: ignore[arg-type]
            destination_id=destination,
        )
    migration = _migration()
    conn = await db_session.connection()

    def round_trip(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()
            migration.upgrade()

    await conn.run_sync(round_trip)

    rows = await db_session.execute(
        text("SELECT trigger_message, channel_id FROM routines ORDER BY trigger_message")
    )
    assert dict(rows.tuples().all()) == {
        "111": "111",
        "222": None,  # a Discord thread's parent needs a platform call
        "C1:1717.5": "C1",
        "None": None,
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO channel_budgets (tenant_id, platform, channel_id, limit_usd, "
                    "\"window\") VALUES (:t, 'slack', 'C1', 5, 'fixed')"
                ),
                {"t": slack.id},
            )
