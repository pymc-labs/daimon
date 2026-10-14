"""The fleet grant expands the existing immutable preference table only."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.testing.factories import make_tenant
from sqlalchemy import inspect, text


@pytest.mark.fresh_schema
async def test_fleet_grant_upgrade_downgrade_upgrade_preserves_backend_rows(db_session):
    path = Path(__file__).parents[2] / "alembic/versions/0096_agent_fleet_grant.py"
    spec = importlib.util.spec_from_file_location("agent_fleet_grant_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0096_agent_fleet_grant"
    assert module.down_revision == "0095_turn_outcome_worker_origin"
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    connection = await db_session.connection()

    def cycle(sync):
        schema = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            sync.execute(
                text(
                    "INSERT INTO agent_backend_revision "
                    "(tenant_id, agent_id, local, backend, agent_name, preview_digest, digest) "
                    "VALUES (:tenant, :agent, 1, 'openai', 'retained', 'preview', 'digest')"
                ),
                {"tenant": tenant.id, "agent": agent_id},
            )
            before = sync.execute(text("SELECT * FROM agent_backend_revision")).mappings().one()
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            column = next(
                column
                for column in inspect(sync).get_columns("agent_backend_revision", schema=schema)
                if column["name"] == "fleet_grant"
            )
            assert column["nullable"] and column["default"] is None
            assert sync.scalar(text("SELECT fleet_grant IS NULL FROM agent_backend_revision"))
            sync.execute(
                text("UPDATE agent_backend_revision SET fleet_grant = CAST(:grant AS jsonb)"),
                {"grant": '{"epoch":1}'},
            )
            assert sync.scalar(text("SELECT fleet_grant FROM agent_backend_revision")) == {
                "epoch": 1
            }
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if "agent_backend_revision" in str(diff)]
            module.downgrade()
            assert "fleet_grant" not in {
                column["name"]
                for column in inspect(sync).get_columns("agent_backend_revision", schema=schema)
            }
            after = sync.execute(text("SELECT * FROM agent_backend_revision")).mappings().one()
            assert dict(after) == dict(before)
            module.upgrade()
            assert sync.scalar(text("SELECT fleet_grant IS NULL FROM agent_backend_revision"))

    await connection.run_sync(cycle)
