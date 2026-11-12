"""Preference migration cycles without altering old channel configurations."""

import importlib.util
import uuid
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


@pytest.mark.fresh_schema
async def test_downgrading_environment_revision_repoints_previous_local_past_selfhosted_rows(
    db_session,
):
    """A revert lands on the choice before the deleted self-hosted revisions, not on nothing."""
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    other_agent = uuid.uuid4()
    chain = [
        # (agent, local, backend, environment, previous_local)
        (agent, 1, "anthropic", None, None),
        (agent, 2, "selfhosted", "sh_env_a", 1),
        (agent, 3, "selfhosted", "sh_env_b", 2),
        (agent, 4, "openai", None, 3),
        (other_agent, 1, "selfhosted", "sh_env_c", None),
        (other_agent, 2, "gemini", None, 1),
    ]
    for owner, local, backend, environment, previous in chain:
        await db_session.execute(
            text(
                "INSERT INTO agent_backend_revision (tenant_id, agent_id, local, backend,"
                " agent_name, preview_digest, digest, previous_local, environment)"
                " VALUES (:t, :a, :l, :b, 'Ada', 'p', 'd', :prev, :env)"
            ),
            {
                "t": tenant.id,
                "a": owner,
                "l": local,
                "b": backend,
                "prev": previous,
                "env": environment,
            },
        )
    path = Path(__file__).parents[1] / "alembic/versions/0202_agent_backend_environment.py"
    spec = importlib.util.spec_from_file_location("agent_backend_environment_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = await db_session.connection()

    def downgrade(sync):
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
        return sync.execute(
            text(
                "SELECT agent_id, local, previous_local FROM agent_backend_revision"
                " ORDER BY agent_id, local"
            )
        ).all()

    rows = await connection.run_sync(downgrade)

    kept = {(row.agent_id, row.local): row.previous_local for row in rows}
    assert kept == {
        (agent, 1): None,
        (agent, 4): 1,
        (other_agent, 2): None,
    }, "self-hosted rows go, and a later revision points at the last one that survives"
