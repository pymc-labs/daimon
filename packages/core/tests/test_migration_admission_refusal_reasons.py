"""The admission refusal reasons migration round-trips the outcome reasons."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_ADDED = (
    "admission_channel_protected",
    "admission_agent_pinned_elsewhere",
    "admission_channel_isolated",
)


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0044_admission_refusal_reasons.py"
    spec = importlib.util.spec_from_file_location("migration_admission_refusal_reasons", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _insert_outcome(session: AsyncSession, reason: str) -> None:
    await session.execute(
        text(
            "INSERT INTO turn_outcomes (id, platform, origin, reason, started_at, ended_at, "
            "duration_ms, recovered, release, usage_refs) VALUES (:id, 'discord', 'chat', "
            ":reason, now(), now(), 0, false, 'test', '[]')"
        ),
        {"id": uuid.uuid4(), "reason": reason},
    )


@pytest.mark.fresh_schema
async def test_downgrade_folds_the_new_reasons_into_admission_denied(
    db_session: AsyncSession,
) -> None:
    for reason in (*_ADDED, "admission_cap_exceeded"):
        await _insert_outcome(db_session, reason)
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(downgrade)
    reasons = await db_session.execute(
        text("SELECT reason, count(*) FROM turn_outcomes GROUP BY 1")
    )
    assert dict(reasons.tuples().all()) == {
        "admission_denied": len(_ADDED),
        "admission_cap_exceeded": 1,
    }, "the new reasons fold back into the generic refusal; others are untouched"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_outcome(db_session, "admission_channel_isolated")

    await conn.run_sync(upgrade)
    for reason in _ADDED:
        await _insert_outcome(db_session, reason)
    count = await db_session.execute(
        text("SELECT count(*) FROM turn_outcomes WHERE reason = 'admission_channel_isolated'")
    )
    assert count.scalar_one() == 1, "the upgrade admits the new reasons again"
