"""Run the unchanged SELECT against an isolated local schema, never production."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from .convert import convert


async def test_sql_cohorts_nulls_window_and_read_only() -> None:
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not dsn:
        pytest.skip("set N9 isolated test database URL for SQL validation")
    parsed = urlparse(dsn)
    assert parsed.hostname in ("localhost", "127.0.0.1")
    assert parsed.path == "/daimon_test_nc_n9", "SQL tests only use N9's local DB"
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
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
