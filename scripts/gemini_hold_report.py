"""Read-only report of outstanding Gemini cap holds per (tenant, account, project).

Run with: uv run python scripts/gemini_hold_report.py --database-url <read-only URL>
(defaults to DAIMON_DATABASE_URL). Opens one READ ONLY transaction and writes
nothing. Pass --hold and --cap (the runtime manifest row's hold_usd and cap_usd)
to also print how many more attempts each ledger admits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import cast

from daimon.core.turn.gemini_state import outstanding_holds
from sqlalchemy import text
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


@asynccontextmanager
async def read_only(connection: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """One transaction in which the database itself rejects any write."""
    async with connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        yield connection


async def read_holds(
    connection: AsyncConnection, *, hold: Decimal | None = None, cap: Decimal | None = None
) -> list[dict[str, object]]:
    """Every hold ledger's outstanding summary, inside one READ ONLY transaction."""
    async with read_only(connection):
        rows = (
            await connection.execute(
                text(
                    "SELECT tenant_id, account_id, project, body FROM gemini_runtime_state "
                    "WHERE kind = 'holds' ORDER BY tenant_id, account_id, project"
                )
            )
        ).mappings()
        lines: list[dict[str, object]] = []
        for row in rows:
            summary = outstanding_holds(cast(str | None, row["body"]))
            line: dict[str, object] = {
                "tenant_id": str(row["tenant_id"]),
                "account_id": str(row["account_id"]),
                "project": row["project"],
                **summary,
            }
            if hold is not None and cap is not None:
                held = Decimal(cast(str, summary["held_usd"]))
                line["attempts_left"] = max(int((cap - held) // hold), 0)
            lines.append(line)
        return lines


async def report(url: str, *, hold: Decimal | None, cap: Decimal | None) -> list[dict[str, object]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await read_holds(connection, hold=hold, cap=cap)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DAIMON_DATABASE_URL"))
    parser.add_argument("--hold", type=Decimal)
    parser.add_argument("--cap", type=Decimal)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DAIMON_DATABASE_URL is required")
    if (args.hold is None) != (args.cap is None) or (args.hold is not None and args.hold <= 0):
        parser.error("--hold and --cap go together, with a positive hold")
    try:
        lines = asyncio.run(report(args.database_url, hold=args.hold, cap=args.cap))
    except ArgumentError:
        # The parse error text would echo the URL, credentials included.
        parser.error("--database-url is not a valid database URL")
    for line in lines:
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
