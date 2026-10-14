"""Preference migration cycles without altering old channel configurations."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.channel_backend import set_channel_backend
from daimon.testing.factories import make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ChannelRef
from sqlalchemy import inspect, text


@pytest.mark.fresh_schema
async def test_upgrade_downgrade_upgrade_preserves_existing_channel_rows(db_session):
    tenant = await make_tenant(db_session)
    channel = ChannelRef(tenant_id=str(tenant.id), platform="discord", channel_id="retained")
    await set_channel_backend(db_session, channel, BackendConfig())
    before = (
        await db_session.execute(text("SELECT revision FROM channel_config_revision"))
    ).scalar_one()
    path = Path(__file__).parents[1] / "alembic/versions/0091_agent_backend_settings.py"
    spec = importlib.util.spec_from_file_location("agent_backend_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = await db_session.connection()

    def cycle(sync):
        schema = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "agent_backend_revision" not in inspect(sync).get_table_names(schema=schema)
            module.upgrade()
            assert "agent_backend_revision" in inspect(sync).get_table_names(schema=schema)
            assert sync.scalar(text("SELECT revision FROM channel_config_revision")) == before
            assert sync.scalar(text("SELECT origin FROM channel_config_revision")) == "channel"
            assert [
                c["name"]
                for c in inspect(sync).get_columns("agent_backend_revision", schema=schema)
            ] == [
                "tenant_id",
                "agent_id",
                "local",
                "backend",
                "agent_name",
                "preview_digest",
                "digest",
                "previous_local",
                "created_at",
            ]

    await connection.run_sync(cycle)
