"""`daimon data init`: set the store up, or put its grants back after a restore."""

from __future__ import annotations

import psycopg
from daimon.core.config import Settings
from daimon.data.dsn import libpq_dsn
from daimon.data.migrate import STORE_FILES, Outcome, apply
from daimon.data.schema import Schemas


class PsycopgSql:
    """`migrate.Sql` over a synchronous psycopg connection."""

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn

    def query(self, sql: str) -> list[tuple[object, ...]]:
        return self._conn.execute(sql.encode()).fetchall()

    def script(self, sql: str) -> None:
        self._conn.execute(sql.encode())  # bytes: simple protocol, several statements


def init_data(settings: Settings, *, repair: bool = False, admin_url: str | None = None) -> Outcome:
    """Apply the store files in one transaction, as `admin_url` or the app's own database user."""
    url = admin_url or str(settings.database.url)
    with psycopg.connect(libpq_dsn(url)) as conn:
        return apply(PsycopgSql(conn), Schemas.of(settings.data), STORE_FILES, repair=repair)
