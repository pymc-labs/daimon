"""The DM watch delivery migration: old DM watches move over, and the downgrade restores them."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Watch
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, select
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0223_dm_watch_delivery.py"
    spec = importlib.util.spec_from_file_location("dm_watch_delivery_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_secret_shares_migration() -> None:
    assert migration().down_revision == "0222_secret_shares"


@pytest.mark.fresh_schema
async def test_old_dm_watches_report_in_the_dm_or_pause_and_the_downgrade_restores_them(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="123"
    )

    def watch(platform: str, thread_id: str) -> Watch:
        return Watch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            owner_account_id=principal.account_id,
            owner_external_id="123",
            platform=platform,
            parent_channel_id="4241",
            thread_id=thread_id,
            agent_id="ag_1",
            agent_name="analyst",
            session_id="session-1",
            delivery="thread",
            instructions="Report changes.",
            producer=f"watch:{uuid.uuid4()}",
            page_id=uuid.uuid4(),
            source="poll",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    discord_dm, discord_thread, slack_dm = (
        watch("discord", "dm:scope"),
        watch("discord", "4242"),
        watch("slack", "dm:scope"),
    )
    db_session.add_all([discord_dm, discord_thread, slack_dm])
    await db_session.flush()
    ids = [w.id for w in (discord_dm, discord_thread, slack_dm)]
    module = migration()
    connection = await db_session.connection()

    def run(sync: Connection, step: str) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            getattr(module, step)()

    async def rows() -> list[tuple[str, str, str | None]]:
        db_session.expire_all()
        found = [
            await db_session.scalar(select(Watch).where(Watch.id == watch_id)) for watch_id in ids
        ]
        return [(w.delivery, w.status, w.session_id) for w in found if w is not None]

    await connection.run_sync(run, "upgrade")
    assert await rows() == [
        ("dm", "active", None),
        ("thread", "active", "session-1"),
        ("thread", "paused", "session-1"),
    ], "Discord reports in the DM, a channel watch is left, Slack pauses"
    await connection.run_sync(run, "downgrade")
    assert await rows() == [("thread", "active", "session-1")] * 3, "all as they were"
