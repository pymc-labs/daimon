"""`daimon data init`: set the store up, or put its grants back after a restore."""

from __future__ import annotations

import psycopg
from daimon.core.config import DataSettings
from daimon.data.dsn import libpq_dsn
from daimon.data.migrate import ROLES, STORE_FILES, Outcome, apply
from daimon.data.schema import Schemas
from psycopg import sql


class PsycopgSql:
    """`migrate.Sql` over a synchronous psycopg connection."""

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn

    def query(self, sql: str) -> list[tuple[object, ...]]:
        return self._conn.execute(sql.encode()).fetchall()

    def script(self, sql: str) -> None:
        self._conn.execute(sql.encode())  # bytes: simple protocol, several statements


def init_data(
    database_url: str, data: DataSettings, *, repair: bool = False, admin_url: str | None = None
) -> Outcome:
    """Apply the store files in one transaction as the app's own database user, who owns the
    store. With `admin_url`, that user first makes only what needs an administrator."""
    with psycopg.connect(libpq_dsn(database_url)) as conn:
        if admin_url:
            row = conn.execute(b"select current_user").fetchone()
            with psycopg.connect(libpq_dsn(admin_url)) as admin:
                _admin_setup(admin, str(row[0] if row else ""))
        return apply(PsycopgSql(conn), Schemas.of(data), STORE_FILES, repair=repair)


def _admin_setup(conn: psycopg.Connection[tuple[object, ...]], app_user: str) -> None:
    """The roles, the extensions only a superuser may add, and what the app's user needs."""
    app = sql.Identifier(app_user)
    for role in ROLES:
        if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("create role {} nologin").format(sql.Identifier(role)))
        conn.execute(sql.SQL("grant {} to {} with admin option").format(sql.Identifier(role), app))
    db = conn.execute(b"select current_database()").fetchone()
    conn.execute(
        sql.SQL("grant create on database {} to {}").format(
            sql.Identifier(str(db[0] if db else "")), app
        )
    )
    for ext in ("vector", "pg_textsearch"):
        try:
            with conn.transaction():
                conn.execute(
                    sql.SQL("create extension if not exists {}").format(sql.Identifier(ext))
                )
        except psycopg.Error:
            pass  # optional: search uses what the server offers
