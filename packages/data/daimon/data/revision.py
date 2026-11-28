"""The Alembic side: run pinned files inside the migration's transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from daimon.core.config import load_data_settings
from daimon.data.migrate import apply
from daimon.data.schema import Schemas, read_sql, render
from sqlalchemy import Connection
from sqlalchemy.util import await_only

log = structlog.get_logger()


class _AlembicSql:
    """Probes through SQLAlchemy and runs scripts on the raw asyncpg connection, which is
    the only way to send several statements at once; both stay in the migration's transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def query(self, sql: str) -> list[tuple[object, ...]]:
        return [tuple(r) for r in self._connection.exec_driver_sql(sql).fetchall()]

    def script(self, sql: str) -> None:
        raw: Any = self._connection.connection.driver_connection
        await_only(raw.execute(sql))


def upgrade(connection: Connection, files: Sequence[str]) -> None:
    """Apply `files`, or log why not and leave Daimon's own migrations to finish.

    A server that lacks what the store needs must not stop Daimon from starting: the store
    runs in a savepoint, so any error it meets rolls back only the store's part.
    `daimon data init` sets the store up later.
    """
    savepoint = connection.begin_nested()
    try:
        outcome = apply(_AlembicSql(connection), Schemas.of(load_data_settings()), files)
    except Exception as exc:  # any refusal stays the store's, never Daimon's
        savepoint.rollback()
        log.warning("data_store_skipped", error=str(exc), fix="daimon data init")
        return
    savepoint.commit()
    if outcome.missing:
        log.warning("data_store_skipped", missing=outcome.missing, fix="daimon data init")


def downgrade(connection: Connection, file: str | None = None) -> None:
    """Undo one store file through its `<name>_down.sql`, where the store is set up; with no
    file, drop the store's schemas. The cluster-wide roles stay: other databases may use them.
    """
    schemas = Schemas.of(load_data_settings())
    if file is None:
        connection.exec_driver_sql(f"drop schema if exists {schemas.views} cascade")
        connection.exec_driver_sql(f"drop schema if exists {schemas.data} cascade")
        return
    sql = _AlembicSql(connection)
    if sql.query(f"select to_regclass('{schemas.data}.schema_version')")[0][0] is not None:
        sql.script(render(read_sql(file.removesuffix(".sql") + "_down.sql"), schemas))
