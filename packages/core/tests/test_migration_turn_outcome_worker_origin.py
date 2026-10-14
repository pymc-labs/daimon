"""A worker turn outcome's origin across an upgrade/downgrade/upgrade."""

from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.testing.factories import make_tenant
from sqlalchemy import CheckConstraint, Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_LEGACY_ORIGINS = ("chat", "handoff", "relay", "routine")
_INSERT = text(
    "INSERT INTO turn_outcomes (id, tenant_id, platform, origin, reason, started_at, "
    "ended_at, duration_ms, recovered, release, usage_refs) VALUES (:id, :tenant, "
    "'discord', :origin, 'completed', :at, :at, 0, false, 'test', '[]'::jsonb)"
)


@pytest.mark.fresh_schema
async def test_worker_origin_upgrade_downgrade_upgrade(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0095_turn_outcome_worker_origin.py"
    spec = importlib.util.spec_from_file_location("turn_outcome_worker_origin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0095_turn_outcome_worker_origin"
    assert module.down_revision == "0094_worker_finished_wakes"

    tenant = await make_tenant(db_session)
    connection = await db_session.connection()

    def insert(sync: Connection, origin: str) -> None:
        sync.execute(
            _INSERT,
            {"id": uuid.uuid4(), "tenant": tenant.id, "origin": origin, "at": datetime.now(UTC)},
        )

    def refused(sync: Connection, origin: str) -> None:
        with pytest.raises(IntegrityError, match="ck_turn_outcomes_origin"), sync.begin_nested():
            insert(sync, origin)

    def origins(sync: Connection) -> tuple[str, ...]:
        return tuple(sync.scalars(text("SELECT origin FROM turn_outcomes ORDER BY origin")))

    def cycle(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            for origin in (*_LEGACY_ORIGINS, "worker"):
                insert(sync, origin)
            refused(sync, "fleet")

            # Down: the worker outcomes go, every other origin's stay, and the
            # previous constraint refuses a worker again.
            module.downgrade()
            assert origins(sync) == _LEGACY_ORIGINS
            refused(sync, "worker")

            module.upgrade()
            insert(sync, "worker")
            assert origins(sync) == (*_LEGACY_ORIGINS, "worker")
            refused(sync, "fleet")

            checks = inspect(sync).get_check_constraints("turn_outcomes", schema=schema)
            migrated = next(c["sqltext"] for c in checks if c["name"] == "ck_turn_outcomes_origin")
            model = next(
                c
                for c in Base.metadata.tables["turn_outcomes"].constraints
                if isinstance(c, CheckConstraint) and c.name == "ck_turn_outcomes_origin"
            )
            assert re.findall(r"'([^']+)'", migrated) == re.findall(
                r"'([^']+)'", str(model.sqltext)
            )
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if "turn_outcomes" in str(diff)]

    await connection.run_sync(cycle)
