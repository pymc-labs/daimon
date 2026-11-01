"""The usage agent backfill names the agent of each untagged row's session."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0158_usage_agent_backfill.py"
    spec = importlib.util.spec_from_file_location("migration_usage_agent_backfill", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _usage(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: str, agent_id: str | None
) -> None:
    await db.execute(
        text(
            "INSERT INTO usage_events (tenant_id, managed_session_id, event_id, agent_id) "
            "VALUES (:t, :s, :e, :a)"
        ),
        {"t": tenant_id, "s": session_id, "e": uuid.uuid4().hex, "a": agent_id},
    )


async def _thread(db: AsyncSession, tenant_id: uuid.UUID, session_id: str, agent_id: str) -> None:
    await db.execute(
        text(
            "INSERT INTO thread_sessions (tenant_id, platform, thread_id, ma_session_id, ma_agent_id) "
            "VALUES (:t, 'discord', :th, :s, :a)"
        ),
        {"t": tenant_id, "th": uuid.uuid4().hex, "s": session_id, "a": agent_id},
    )


async def _outcome(db: AsyncSession, tenant_id: uuid.UUID, session_id: str, agent_id: str) -> None:
    await db.execute(
        text(
            "INSERT INTO turn_outcomes (id, tenant_id, platform, agent_id, session_id, origin, reason, "
            "started_at, ended_at) VALUES (:id, :t, 'discord', :a, :s, 'mention', 'completed', now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant_id, "s": session_id, "a": agent_id},
    )


async def test_backfill_tags_rows_whose_session_has_one_agent(db_session: AsyncSession) -> None:
    tenant = (await make_tenant(db_session)).id
    other = (await make_tenant(db_session)).id
    await _thread(db_session, tenant, "sesn_thread", "agent_a")
    await _outcome(db_session, tenant, "sesn_outcome", "agent_b")
    await _thread(db_session, tenant, "sesn_split", "agent_a")
    await _outcome(db_session, tenant, "sesn_split", "agent_b")
    await _thread(db_session, other, "sesn_foreign", "agent_c")
    for session_id in (
        "sesn_thread",
        "sesn_outcome",
        "sesn_split",
        "sesn_foreign",
        "thread-naming:1",
    ):
        await _usage(db_session, tenant, session_id, None)
    await _usage(db_session, tenant, "sesn_thread", "agent_kept")

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            _migration().upgrade()

    await (await db_session.connection()).run_sync(upgrade)
    rows = await db_session.execute(
        text("SELECT managed_session_id, agent_id FROM usage_events ORDER BY 1, 2")
    )
    assert sorted(rows.tuples(), key=str) == sorted(
        [
            ("sesn_thread", "agent_a"),
            ("sesn_thread", "agent_kept"),
            ("sesn_outcome", "agent_b"),
            ("sesn_split", None),
            ("sesn_foreign", None),
            ("thread-naming:1", None),
        ],
        key=str,
    ), "only rows whose session names one agent in the same tenant are tagged"
