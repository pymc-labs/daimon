"""Evals and probes: grading as each caller reads, the spend stop, and probes that write nothing."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from daimon.core.config import DataSettings
from daimon.data import embed, probes
from daimon.data.access import person
from daimon.data.evals import gold
from daimon.data.evals.run import Budget, estimate, evaluate
from daimon.data.schema import Schemas
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
EXAMPLE = Path(__file__).with_name("gold_example.json")
MODEL = "text-embedding-3-small"
EMBED = DataSettings(embed_url="https://embed.example.com/v1", embed_model=MODEL, embed_dim=8)
CACHES = ("read_contexts", "audience_sets", "audience_acls")
"""What every read refills; not store state."""


@dataclass
class Ledger:
    spent: list[int] = field(default_factory=list[int])

    async def allowed(self, tenant_id: uuid.UUID) -> bool:
        return True

    def priced(self, model: str) -> bool:
        return True

    async def record(
        self,
        purpose: str,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        model: str,
        units: int,
    ) -> None:
        self.spent.append(units)


def fake_embeddings(request: httpx.Request) -> httpx.Response:
    texts: list[str] = json.loads(request.content)["input"]
    data = [{"index": i, "embedding": [0.1] * 8} for i, _ in enumerate(texts)]
    return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7 * len(texts)}})


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas

    async def put(self, src: uuid.UUID, ext: str, scope: str, text: str) -> None:
        d = self.schemas.data
        cur = await self.conn.execute(
            f"insert into {d}.records (source_id, external_id, kind, title, body, occurred_at, scope)"
            " values (%s, %s, 'doc', %s, %s, now(), %s) returning id".encode(),
            (src, ext, text, text, scope),
        )
        row = await cur.fetchone()
        assert row is not None, "a record insert returns its id"
        await self.conn.execute(
            f"insert into {d}.chunks (record_id, source_id, ord, text, scope)"
            " values (%s, %s, 0, %s, %s)".encode(),
            (row["id"], src, text, scope),
        )


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[tuple[Store, uuid.UUID]]:
    """The example's tenant with its three records; its rows go at the end."""
    async with data_db.aconnect() as conn:
        d = data_db.schemas.data
        cur = await conn.execute(
            f"insert into {d}.sources (tenant_id, kind, name, default_scope, publication)"
            " values (%s, 'docs', 'docs', 'internal', 'published') returning id".encode(),
            (TENANT,),
        )
        row = await cur.fetchone()
        assert row is not None, "a source insert returns its id"
        s = Store(conn, data_db.schemas)
        await s.put(row["id"], "heron", "internal", "heron colony nests by the lake")
        await s.put(row["id"], "heron-secret", "restricted:board", "heron colony nest count")
        await s.put(row["id"], "soup", "internal", "cafeteria soup of the day")
        try:
            yield s, row["id"]
        finally:
            await conn.execute(f"delete from {d}.sources where tenant_id = %s".encode(), (TENANT,))


def flat_price(model: str, units: int) -> float:
    return 1.0


async def test_grading_counts_only_expected_records_the_caller_can_read(
    store: tuple[Store, uuid.UUID],
) -> None:
    s, _ = store
    report = await evaluate(
        s.conn,
        s.schemas,
        DataSettings(),
        gold.load(EXAMPLE),
        meter=Ledger(),
        budget=Budget(1.0, flat_price),
        estimated=0.0,
    )
    rows = {r["id"]: r for r in report["rows"]}
    assert (rows["nests"]["expected"], rows["nests"]["visible"]) == (2, 1), (
        "the record in a scope the caller lacks is access coverage, not an expected miss"
    )
    assert rows["nests"]["metrics"]["fused"]["recall@5"] == 1.0, "found what the caller can read"
    assert "metrics" not in rows["outsider"], "a caller who can read none of it is not graded"
    assert (report["graded"], report["ungraded"]) == (2, 1), "the summary says what it left out"
    assert set(report["arms"]) >= {"fused", "fts"}, "each arm is reported beside the fused list"


async def test_the_run_stops_once_metered_spend_reaches_the_cap(
    store: tuple[Store, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    s, _ = store
    monkeypatch.setattr(embed, "transport", httpx.MockTransport(fake_embeddings))
    questions = gold.load(EXAMPLE).questions[:2]
    assert estimate(questions, EMBED, flat_price) == 2.0, "one embedding per question"
    ledger = Ledger()
    report = await evaluate(
        s.conn,
        s.schemas,
        EMBED,
        gold.GoldSet(questions=questions),
        meter=ledger,
        budget=Budget(1.0, flat_price),
        estimated=2.0,
    )
    assert (report["ran"], len(ledger.spent)) == (1, 1), "the second question never started"
    assert report["halted"], "the report says it stopped early"
    assert report["spend"]["metered_usd"] == 1.0, "the cost is what the meter recorded"


async def snapshot(conn: AsyncConnection[DictRow], d: str) -> dict[str, int]:
    names = await (
        await conn.execute(b"select tablename from pg_tables where schemaname = %s", (d,))
    ).fetchall()
    counts: dict[str, int] = {}
    for r in names:
        if r["tablename"] not in CACHES:
            cur = await conn.execute(f"select count(*) as n from {d}.{r['tablename']}".encode())
            counts[r["tablename"]] = (await cur.fetchone() or {"n": -1})["n"]
    epoch = await (await conn.execute(f"select n from {d}.acl_epoch".encode())).fetchone()
    return counts | {"epoch": epoch["n"] if epoch else -1}


async def test_probes_read_the_store_and_write_nothing(store: tuple[Store, uuid.UUID]) -> None:
    s, _ = store
    d, account = s.schemas.data, uuid.uuid4()
    await s.conn.execute(
        f"insert into {d}.scopes (tenant_id, id, kind, name) values (%s, 'internal', 'internal', 'x')".encode(),
        (TENANT,),
    )
    await s.conn.execute(
        f"insert into {d}.grants (tenant_id, scope, holder) values (%s, 'internal', %s)".encode(),
        (TENANT, person(account)),
    )
    before = await snapshot(s.conn, d)
    found = {n: await probes.run(n, s.conn, s.schemas, TENANT, account) for n in probes.NAMES}
    assert await snapshot(s.conn, d) == before, "no probe changed a table"
    assert [x["records"] for x in found["person"]["sources"]] == [2], (
        "the person reads their scope only"
    )
    assert [x["scope"] for x in found["scopes"]["undefined"]] == ["restricted:board"], (
        "undefined scope found"
    )
    await s.conn.execute(f"delete from {d}.scopes where tenant_id = %s".encode(), (TENANT,))
