"""Run the unchanged SELECT against an isolated local schema, never production."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from .convert import convert


def local_isolated_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if (
        parsed.scheme not in ("postgresql", "postgresql+asyncpg")
        or parsed.hostname not in ("localhost", "127.0.0.1")
        or not re.fullmatch(r"/daimon_test_nc_[a-z0-9][a-z0-9_]{0,47}", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("SQL validation requires an isolated neutral-core DB on localhost")
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.parametrize("lane", ["n9", "eval_n9", "review_n9_f390"])
def test_sql_accepts_isolated_worker_and_reviewer_databases(lane: str) -> None:
    dsn = f"postgresql+asyncpg://localhost/daimon_test_nc_{lane}"
    assert local_isolated_dsn(dsn) == f"postgresql://localhost/daimon_test_nc_{lane}"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://localhost/daimon_test",
        "postgresql://localhost/daimon_test_nc_",
        "postgresql://remote.invalid/daimon_test_nc_n9",
        "postgresql://localhost/production",
        "postgresql://localhost/daimon_test_nc_n9?host=remote.invalid",
        "postgresql://localhost/daimon_test_nc_n9#fragment",
        "mysql://localhost/daimon_test_nc_n9",
        "postgresql://localhost/daimon_test_nc_" + "n" * 49,
    ],
)
def test_sql_rejects_shared_remote_or_overridden_databases(dsn: str) -> None:
    with pytest.raises(ValueError, match="isolated neutral-core DB"):
        local_isolated_dsn(dsn)


async def test_sql_cohorts_nulls_window_and_read_only() -> None:
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not dsn:
        pytest.skip("set an isolated neutral-core test database URL for SQL validation")
    conn = await asyncpg.connect(local_isolated_dsn(dsn))
    schema = "baseline_n9_" + uuid.uuid4().hex
    sql = (Path(__file__).parent / "telemetry.sql").read_text()
    query = (
        sql.split("-- BEGIN QUERY\n", 1)[1]
        .split("-- END QUERY", 1)[0]
        .replace("public.", f'"{schema}".')
    )
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'''CREATE TABLE "{schema}".usage_events (
            occurred_at timestamptz, model text, input_tokens bigint,
            cache_read_input_tokens bigint, cache_creation_input_tokens bigint,
            output_tokens bigint)''')
        await conn.execute(f'''CREATE TABLE "{schema}".turn_outcomes (
            id integer, started_at timestamptz, duration_ms bigint, model_ids jsonb)''')
        await conn.execute(f'''INSERT INTO "{schema}".usage_events VALUES
            (now() - interval '1 day', 'm1', 10, 20, 30, 40),
            (now() - interval '2 days', 'm1', 1, 2, 3, 4),
            (now() - interval '15 days', 'm1', 1000, 0, 0, 0),
            (now() + interval '1 day', 'm1', 1000, 0, 0, 0),
            (now() - interval '1 day', 'only-usage', 5, 0, 0, 0)''')
        await conn.execute(f'''INSERT INTO "{schema}".turn_outcomes VALUES
            (1, now() - interval '1 day', 100, '["m1","m1","only-turns"]'),
            (2, now() - interval '1 day', 200, '["m1"]'),
            (3, now() - interval '15 days', 9999, '["m1"]')''')
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL TIME ZONE 'UTC'")
            value = await conn.fetchval(query)
            assert await conn.fetchval("SHOW transaction_read_only") == "on"
        document = json.loads(value)
        rows = {r["model"]: r for r in document["models"]}
        assert rows["m1"]["usage_events"] == 2 and rows["m1"]["uncached_input_tokens"] == 11
        assert rows["m1"]["turns"] == 2 and rows["m1"]["p50_total_ms"] == 150
        assert rows["m1"]["p95_total_ms"] == 195
        assert rows["only-usage"]["turns"] == 0 and rows["only-usage"]["p50_total_ms"] is None
        assert (
            rows["only-turns"]["usage_events"] == 0
            and rows["only-turns"]["uncached_input_tokens"] is None
        )
        assert all(r["p50_first_token_ms"] is None for r in rows.values())
        baseline = convert(
            value,
            sdk_pin="anthropic==0.117.0",
            baseline_date=date.fromisoformat(document["window_end"][:10]),
        )
        assert baseline["schema_version"] == 1
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
