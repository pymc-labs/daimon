"""The `data_db` fixture: a rendered data store per worker, beside the worker's core schema.

Import it in a conftest next to `db_engine` and `db_schema`:

    from daimon.testing.data_db import data_db  # noqa: F401

The store lives in `<worker>_data` and `<worker>_data_views`, so xdist workers and
deployments never share one. The `daimon_data_*` roles are cluster-wide; `apply` creates
them under one server-wide lock. Needs a database whose role can create roles and, for the
vector tests, the `vector` extension (the pgvector image, as CI's data leg uses). Only tests
that use this fixture create the schemas; the default suites never do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

import psycopg
import pytest_asyncio
from daimon.core.config import DataSettings
from daimon.data.dsn import libpq_dsn
from daimon.data.init import PsycopgSql
from daimon.data.locks import CLUSTER, advisory_key
from daimon.data.migrate import STORE_FILES, apply
from daimon.data.schema import Schemas
from daimon.testing.db import DatabasePreflight
from psycopg.rows import DictRow, dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_DROP_CHUNK_SIZE = 4


@dataclass(frozen=True)
class DataDb:
    """The worker's store: its schemas, settings and a way to open a connection."""

    url: str = field(repr=False)
    schemas: Schemas
    settings: DataSettings

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        """A psycopg connection in autocommit, as the database's own user."""
        with psycopg.connect(libpq_dsn(self.url), autocommit=True) as conn:
            yield conn

    @asynccontextmanager
    async def aconnect(self) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
        """An async psycopg connection in autocommit with dict rows, as the store's code takes."""
        conn = await psycopg.AsyncConnection[DictRow].connect(
            libpq_dsn(self.url), autocommit=True, row_factory=dict_row
        )
        async with conn:
            yield conn


@pytest_asyncio.fixture(scope="session")
async def data_db(
    db_preflight: DatabasePreflight, db_engine: AsyncEngine, db_schema: str
) -> AsyncIterator[DataDb]:
    """Apply the store files to this worker's own schemas; drop them at the end."""
    settings = DataSettings(schema_name=f"{db_schema}_data")
    store = DataDb(db_preflight.url, Schemas.of(settings), settings)
    with store.connect() as conn, conn.transaction():
        # intarray goes in the first store's schema, once per database; keep it in a schema no
        # worker's teardown drops, so every worker's store uses it there.
        conn.execute("select pg_advisory_xact_lock(%s)", (advisory_key(CLUSTER, "migrate"),))
        conn.execute(
            b"create schema if not exists data_test_ext;"
            b" grant usage on schema data_test_ext to public;"
            b" create extension if not exists intarray schema data_test_ext"
        )
    with store.connect() as conn:
        apply(PsycopgSql(conn), store.schemas, STORE_FILES)
    try:
        yield store
    finally:
        await _drop(db_engine, store.schemas)


async def _drop(engine: AsyncEngine, schemas: Schemas) -> None:
    """Drop the views, then the tables a few per transaction, then both schemas."""
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schemas.views}" CASCADE'))
    async with engine.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                        "WHERE schemaname = :s ORDER BY tablename DESC"
                    ),
                    {"s": schemas.data},
                )
            )
            .scalars()
            .all()
        )
    for start in range(0, len(tables), _DROP_CHUNK_SIZE):
        async with engine.begin() as conn:
            names = ", ".join(tables[start : start + _DROP_CHUNK_SIZE])
            await conn.execute(text(f"DROP TABLE IF EXISTS {names} CASCADE"))
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schemas.data}" CASCADE'))
