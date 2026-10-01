"""Drop orphaned test schemas from the daimon test database.

By default drops ``test_[wf]<pid>_<8hex>`` schemas whose owning pytest worker
is gone: no backend reports the schema as ``application_name`` and the pid is
not alive on this host (the rule ``daimon.testing.db.sweep_orphan_schemas``
applies at engine setup, here without the per-run bound).

``--include-legacy`` also drops the ``test_<32hex>`` schemas left by the old
schema-per-test fixture, but only when ``pg_stat_activity`` shows no other
connection to the database — a worktree still on that fixture could be using
them. Each schema is dropped in its own transaction so the lock table is
never asked for hundreds of drops at once.

Usage (reads DAIMON_DATABASE__TEST_URL):

    uv run python scripts/db/sweep_test_schemas.py [--include-legacy]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from urllib.parse import urlparse

from daimon.testing.db import sweep_orphan_schemas
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_LEGACY_PATTERN = r"^test_[0-9a-f]{32}$"


def _test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        sys.exit("DAIMON_DATABASE__TEST_URL must be set.")
    db_name = urlparse(url).path.lstrip("/")
    if "test" not in db_name:
        sys.exit(f"Refusing to sweep database {db_name!r}: its name must contain 'test'.")
    return url


async def _sweep_legacy(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        others = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()
        if others:
            sys.exit(
                f"Refusing to drop legacy schemas: {others} other connection(s) to the "
                "database (a test run on the old fixture may still be using them)."
            )
        legacy: Sequence[str] = (
            (
                await conn.execute(
                    text("SELECT nspname FROM pg_namespace WHERE nspname ~ :p ORDER BY nspname"),
                    {"p": _LEGACY_PATTERN},
                )
            )
            .scalars()
            .all()
        )
    for schema in legacy:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    return list(legacy)


async def _main(*, include_legacy: bool) -> None:
    engine = create_async_engine(_test_dsn())
    try:
        dropped = await sweep_orphan_schemas(engine, limit=1_000_000)
        print(f"dropped {len(dropped)} orphaned worker schema(s)")
        for schema in dropped:
            print(f"  {schema}")
        if include_legacy:
            legacy = await _sweep_legacy(engine)
            print(f"dropped {len(legacy)} legacy test_<32hex> schema(s)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="also drop test_<32hex> schemas when no other connection is open",
    )
    args = parser.parse_args()
    asyncio.run(_main(include_legacy=args.include_legacy))
