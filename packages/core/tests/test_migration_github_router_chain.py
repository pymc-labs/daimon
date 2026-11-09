"""0174a and 0186 share the router DDL; every path to head must survive it.

Production reaches 0174a from a release branch, staging already ran the DDL as
0186, and a fresh database runs it once under 0174a.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from .test_migration_chain import HEAD, SCRIPTS, _assert_head, _config, migration_database

__all__ = ["migration_database"]

ROUTER = "0174a_github_router_sessions"
SESSION_TABLE = "github_router_sessions"
SESSION_INDEXES = {
    "ix_github_router_sessions_open_ma_session",
    "ix_github_router_sessions_open_account",
}
TOKEN_INDEX = "ix_github_issued_tokens_router_session"


def _assert_router_objects(sync: Connection) -> None:
    inspector = inspect(sync)
    assert inspector.has_table(SESSION_TABLE)
    assert {i["name"] for i in inspector.get_indexes(SESSION_TABLE)} >= SESSION_INDEXES
    assert "router_session_id" in {c["name"] for c in inspector.get_columns("github_issued_tokens")}
    assert TOKEN_INDEX in {i["name"] for i in inspector.get_indexes("github_issued_tokens")}


async def test_empty_database_upgrades_to_head(migration_database: AsyncEngine) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            command.upgrade(_config(SCRIPTS, sync), "head")
            _assert_head(sync)
            _assert_router_objects(sync)

        await connection.run_sync(exercise)


async def test_production_stamp_at_0174a_upgrades_to_head(
    migration_database: AsyncEngine,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config, ROUTER)
            assert sync.scalar(text("SELECT version_num FROM alembic_version")) == ROUTER
            _assert_router_objects(sync)
            sync.commit()
            command.upgrade(config, "head")
            _assert_head(sync)
            _assert_router_objects(sync)

        await connection.run_sync(exercise)


async def test_staging_at_0186_with_tables_reruns_and_downgrades(
    migration_database: AsyncEngine,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config, "head")
            sync.commit()
            # Downgrading past 0186 keeps 0174a's router schema: production
            # rolled back to its release revision still needs it.
            command.downgrade(config, "0185_agent_post_role")
            sync.commit()
            _assert_router_objects(sync)
            command.downgrade(config, "0174a_github_router_sessions")
            sync.commit()
            _assert_router_objects(sync)
            command.upgrade(config, "head")
            _assert_head(sync, HEAD)
            _assert_router_objects(sync)
            sync.commit()
            # Objects present, version behind: 0186 must skip all of them. Later
            # revisions are undone first, so only 0186's own objects are left over.
            command.downgrade(config, "0186_github_router_sessions")
            sync.commit()
            sync.execute(text("UPDATE alembic_version SET version_num = '0185_agent_post_role'"))
            sync.commit()
            command.upgrade(config, "head")
            _assert_head(sync, HEAD)
            _assert_router_objects(sync)
            sync.commit()
            # Downgrading below 0174a removes the schema it owns.
            command.downgrade(config, "0174_drop_channel_admins")
            assert not inspect(sync).has_table(SESSION_TABLE)

        await connection.run_sync(exercise)
