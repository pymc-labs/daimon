"""0169 settles the wake reasons to the union of both branches, without Fleet's."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
REASONS = {
    "task_handoff",
    "private_input_applied",
    "timer",
    "github_access_ready",
    "watch_fired",
    "job_finished",
    "watch_report",
}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "continuation_reasons", _VERSIONS / "0169_continuation_reasons.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reason_check(sync: Connection) -> tuple[str, bool]:
    row = sync.execute(
        text(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conname = 'ck_task_continuations_reason' "
            "AND conrelid = 'task_continuations'::regclass"
        )
    ).one()
    return row[0], row[1]


def _allowed(definition: str) -> set[str]:
    return {reason for reason in [*REASONS, "worker_finished"] if f"'{reason}'" in definition}


@pytest.mark.fresh_schema
async def test_upgrade_sets_the_union_without_worker_finished_and_round_trips(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    migration = _module()

    def cycle(sync: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            migration.upgrade()
            definition, validated = _reason_check(sync)
            assert _allowed(definition) == REASONS
            # NOT VALID keeps historical worker_finished rows readable.
            assert not validated
            migration.downgrade()
            definition, _ = _reason_check(sync)
            assert _allowed(definition) == REASONS | {"worker_finished"}
            migration.upgrade()
            definition, validated = _reason_check(sync)
            assert _allowed(definition) == REASONS
            assert not validated

    await connection.run_sync(cycle)
