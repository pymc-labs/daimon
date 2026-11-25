"""Sync: the external Postgres connector end to end, the network guard, the scheduler, and the
search and reads over what it synced."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from daimon.core.config import DataSettings
from daimon.data import embed, reads
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.caller_sql import SqlRefused, data_sql
from daimon.data.dsn import libpq_dsn
from daimon.data.metering import Payer
from daimon.data.schema import Schemas
from daimon.data.search import Filters, search
from daimon.data.sync import connectors  # noqa: F401  # pyright: ignore[reportUnusedImport]
from daimon.data.sync.attachments import text_of
from daimon.data.sync.credentials import seal
from daimon.data.sync.embedding import embed_pending
from daimon.data.sync.engine import Env, run_source
from daimon.data.sync.netguard import NotPublic, resolve
from daimon.data.sync.rate import RateBudget
from daimon.data.sync.scheduler import due, recover, take_lead
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()


@dataclass
class World:
    """A store, an external database (a schema in the same server) and a reader role for it."""

    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    ext: str
    dsn: str
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def sql(self, query: str, *params: object) -> list[DictRow]:
        cur = await self.conn.execute(query.encode(), params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall() if cur.description else []

    async def source(self, **config: object) -> uuid.UUID:
        config = {"view": f"{self.ext}.docs_v", "kind": "doc", **config}
        rows = await self.sql(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, config, credential,"
            " default_scope, publication) values (%s, 'postgres', 'ext', 'ext', %s::jsonb, %s,"
            " 'internal', 'published') returning id",
            self.tenant,
            Jsonb(config),
            seal((KEY,), {"dsn": self.dsn}),
        )
        return rows[0]["id"]

    async def put(self, id_: int, title: str, readers: list[str] | None = None) -> None:
        await self.sql(
            f"insert into {self.ext}.docs (id, title, body, updated_at, readers)"
            " values (%s, %s, %s, now(), %s) on conflict (id) do update"
            " set title = excluded.title, updated_at = clock_timestamp()",
            id_,
            title,
            f"body of {title}",
            readers or [],
        )

    async def ids(self, src: uuid.UUID) -> set[str]:
        rows = await self.sql(f"select external_id from {self.d}.records where source_id = %s", src)
        return {r["external_id"] for r in rows}


@pytest.fixture
async def world(data_db: DataDb) -> AsyncIterator[World]:
    ext, role = f"ext_{uuid.uuid4().hex[:8]}", f"ext_reader_{uuid.uuid4().hex[:8]}"
    parts = conninfo_to_dict(libpq_dsn(data_db.url))
    dsn = make_conninfo(libpq_dsn(data_db.url), user=role, password="reader")
    async with data_db.aconnect() as conn:
        w = World(
            conn,
            data_db.schemas,
            Env(data_db.schemas, (KEY,), (str(parts["host"]),)),
            ext,
            dsn,
            uuid.uuid4(),
        )
        await w.sql(f"create schema {ext}")
        await w.sql(
            f"create table {ext}.docs (id int primary key, title text, body text,"
            " updated_at timestamptz, readers text[])"
        )
        await w.sql(f"create view {ext}.docs_v as select * from {ext}.docs")
        await w.sql(f"create role {role} login password 'reader' nosuperuser nobypassrls")
        await w.sql(f"grant usage on schema {ext} to {role}")
        await w.sql(f"grant select on {ext}.docs_v to {role}")
        try:
            yield w
        finally:
            await w.sql(f"drop schema {ext} cascade")
            await w.sql(f"drop role {role}")


async def test_sync_updates_and_prunes_what_left_the_view(world: World) -> None:
    src = await world.source()
    for i in (1, 2, 3):
        await world.put(i, f"doc {i}")

    first = await run_source(world.conn, world.env, src)
    assert (first.status, first.added, first.error) == ("ok", 3, None), "all three are new"
    assert await world.ids(src) == {"doc:1", "doc:2", "doc:3"}, "a record is `<kind>:<id>`"
    chunks = await world.sql(
        f"select count(*) as n from {world.d}.chunks where source_id = %s", src
    )
    assert chunks[0]["n"] >= 3, "each record is chunked"
    cursor = await world.sql(
        f"select state from {world.d}.source_cursors where source_id = %s", src
    )
    assert cursor[0]["state"]["cursor"], "the cursor is kept"

    again = await run_source(world.conn, world.env, src)
    assert (again.added, again.updated, again.deleted) == (0, 0, 0), (
        "nothing changed: nothing written"
    )

    await world.put(2, "doc 2 changed")
    await world.put(4, "doc 4")
    await world.sql(f"delete from {world.ext}.docs where id = 3")
    third = await run_source(world.conn, world.env, src)
    assert (third.added, third.updated, third.deleted) == (1, 1, 1), (
        "one each of new, changed, gone"
    )
    assert await world.ids(src) == {"doc:1", "doc:2", "doc:4"}, "the deleted row left the store"


async def test_an_empty_view_is_not_believed(world: World) -> None:
    src = await world.source()
    await world.put(1, "doc 1")
    await run_source(world.conn, world.env, src)
    await world.sql(f"delete from {world.ext}.docs")

    result = await run_source(world.conn, world.env, src)
    assert result.status == "error", "an empty list after records is a failed read until proven"
    assert await world.ids(src) == {"doc:1"}, "nothing was removed"


async def test_a_readers_column_is_the_access_list(world: World) -> None:
    src = await world.source(readers={"column": "readers", "workspace": "ws", "kind": "user"})
    await world.put(1, "doc 1", readers=["ann"])
    result = await run_source(world.conn, world.env, src)
    assert result.status == "ok", result.error

    async def sees(who: str) -> int:
        ctx = ReadContext((world.tenant,), readers=(subject("ext", "ws", "user", who),))
        async with scoped(world.conn, world.schemas, ctx) as c:
            rows = await (await c.execute(f"select id from {world.d}.records".encode())).fetchall()
        return len(rows)

    assert await sees("ann") == 1, "the listed reader reads it"
    assert await sees("bob") == 0, "anyone else does not"


async def test_a_private_host_is_refused_unless_listed(world: World) -> None:
    src = await world.source()
    await world.put(1, "doc 1")
    strict = Env(world.schemas, (KEY,), ())
    result = await run_source(world.conn, strict, src)
    assert result.status == "error", "a database on a private address is not reached"
    assert await world.ids(src) == set(), "and nothing was stored"


async def test_resolve_allows_only_public_or_listed_hosts() -> None:
    with pytest.raises(NotPublic):
        await resolve("127.0.0.1", 5432)
    with pytest.raises(NotPublic):
        await resolve("db.internal", 5432)
    assert await resolve("127.0.0.1", 5432, ("127.0.0.0/8",)) == "127.0.0.1"


async def test_text_is_read_from_a_text_file_and_a_docx() -> None:
    assert await text_of("a.txt", "text/plain", b"hello") == "hello"
    assert await text_of("a.pdf", "application/pdf", b"%PDF") is None, "binaries are listed only"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>quarterly plan</w:t></w:r></w:p></w:body></w:document>",
        )
    got = await text_of("plan.docx", "", buf.getvalue())
    assert got and "quarterly plan" in got, "the sandbox returns the document's words"


async def test_a_rate_budget_waits_for_its_tokens() -> None:
    now = [0.0]
    budget = RateBudget(60.0, clock=lambda: now[0])
    await budget.take()  # the burst
    now[0] = 1.0
    await budget.take()  # one a second refills one
    assert budget._tokens == 0, "a spent token is gone"  # pyright: ignore[reportPrivateUsage]


async def test_one_scheduler_leads_and_a_cut_run_is_recovered(
    world: World, data_db: DataDb
) -> None:
    first = await take_lead(data_db.url, world.schemas)
    assert first is not None, "the lock was free"
    assert await take_lead(data_db.url, world.schemas) is None, "a second leader is refused"
    await first.close()
    again = await take_lead(data_db.url, world.schemas)
    assert again is not None, "the lock went with its connection"
    await again.close()

    src = await world.source()
    assert src in [j.id for j in await due(world.conn, world.schemas)], "never synced: due"
    await world.put(1, "doc 1")
    await run_source(world.conn, world.env, src)
    now = datetime.now(UTC)
    assert src not in [j.id for j in await due(world.conn, world.schemas, now)], "just synced"
    later = now + timedelta(hours=2)
    assert src in [j.id for j in await due(world.conn, world.schemas, later)], "past its interval"

    await world.sql(
        f"insert into {world.d}.sync_runs (source_id, started_at) values (%s, now() - interval '1 hour')",
        src,
    )
    assert await recover(world.conn, world.schemas) == 1, "the run nobody holds is closed"
    left = await world.sql(
        f"select count(*) as n from {world.d}.sync_runs where source_id = %s and status = 'running'",
        src,
    )
    assert left[0]["n"] == 0, "no run is left running"


MODEL = "text-embedding-3-small"
WORDS = ("heron", "colony", "soup", "cafeteria", "fish", "lake", "nests", "frogs")


def fake_embeddings(request: httpx.Request) -> httpx.Response:
    """An embeddings API of word counts over eight dimensions: texts sharing words are near."""
    texts: list[str] = json.loads(request.content)["input"]
    data = [
        {"index": i, "embedding": [float(t.lower().count(w)) + 0.01 for w in WORDS]}
        for i, t in enumerate(texts)
    ]
    return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7 * len(texts)}})


@dataclass
class Ledger:
    """A meter that keeps what it is asked to bill."""

    open: bool = True
    spent: list[tuple[str, uuid.UUID, str | None, int]] = field(
        default_factory=list[tuple[str, uuid.UUID, str | None, int]]
    )

    async def allowed(self, tenant_id: uuid.UUID) -> bool:
        return self.open

    def priced(self, model: str) -> bool:
        return model == MODEL

    async def record(
        self,
        purpose: str,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        model: str,
        units: int,
    ) -> None:
        self.spent.append((purpose, tenant_id, platform_user_id, units))


EMBED = DataSettings(embed_url="https://embed.example.com/v1", embed_model=MODEL, embed_dim=8)


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "transport", httpx.MockTransport(fake_embeddings))


async def put_docs(world: World, src: uuid.UUID) -> None:
    for i, title in enumerate(("heron colony nests", "cafeteria soup", "lake fish frogs"), 1):
        await world.put(i, title)
    await run_source(world.conn, replace(world.env, embed=False), src)  # synced, not yet embedded


async def test_search_ranks_by_text_and_vector_and_bills_ingest_and_the_asker(
    world: World, embedder: None
) -> None:
    ledger = Ledger()
    src = await world.source()
    for i, title in enumerate(("heron colony nests", "cafeteria soup", "lake fish frogs"), 1):
        await world.put(i, title)
    env = replace(world.env, embed=True, data=EMBED, meter=ledger)
    await run_source(world.conn, env, src)
    counts = await world.sql(
        f"select count(v.chunk_id) as vectors, count(*) as chunks from {world.d}.chunks c"
        f" left join {world.d}.chunk_vectors v on v.chunk_id = c.id where c.source_id = %s",
        src,
    )
    assert counts[0]["vectors"] == counts[0]["chunks"] > 0, "every chunk got a vector"
    assert [(p, t) for p, t, _, _ in ledger.spent] == [("data_embedding", world.tenant)], (
        "ingest is billed once, to the source's tenant"
    )

    asker = Payer(uuid.uuid4(), "u1")
    ctx = ReadContext(tenant_ids=(world.tenant,), scopes=("internal",))
    found = await search(
        world.conn, world.schemas, ctx, "where does the heron colony nest",
        settings=EMBED, meter=ledger, payer=asker, limit=3,
    )  # fmt: skip
    assert found.hits[0].title == "heron colony nests", "the matching record ranks first"
    assert set(found.hits[0].arms) == {"fts", "vec"}, "both arms found it"
    assert ledger.spent[-1][:3] == ("data_embedding", asker.tenant_id, "u1"), (
        "the query is billed to the asker, not the source's tenant"
    )
    dated = await search(
        world.conn, world.schemas, ctx, "heron colony today", settings=EMBED, limit=3
    )
    assert "time" in dated.hits[0].arms, "a date phrase adds the time arm"
    none = await search(
        world.conn, world.schemas, ctx, "heron", settings=EMBED, filters=Filters(kinds=("other",))
    )
    assert none.hits == [], "a filter on another kind finds nothing"


async def test_an_empty_balance_falls_back_to_full_text_and_waits_to_embed(
    world: World, embedder: None
) -> None:
    ledger = Ledger(open=False)
    src = await world.source()
    await put_docs(world, src)
    ctx = ReadContext(tenant_ids=(world.tenant,), scopes=("internal",))

    found = await search(
        world.conn, world.schemas, ctx, "heron colony",
        settings=EMBED, meter=ledger, payer=Payer(uuid.uuid4()),
    )  # fmt: skip
    assert [set(h.arms) for h in found.hits] == [{"fts"}], "full text answers alone"
    assert "balance" in found.notes, "the answer says why"
    assert ledger.spent == [], "nothing was billed"

    assert await embed_pending(world.conn, world.schemas, src, settings=EMBED, meter=ledger) == 0, (
        "a tenant out of balance keeps its gaps"
    )
    ledger.open = True
    waiting = await world.sql(
        f"select count(*) as n from {world.d}.chunks where source_id = %s", src
    )
    assert (
        await embed_pending(world.conn, world.schemas, src, settings=EMBED, meter=ledger)
        == waiting[0]["n"]
    ), "they fill once it tops up"


async def test_search_without_an_embeddings_endpoint_is_full_text_only(world: World) -> None:
    src = await world.source()
    await put_docs(world, src)
    ctx = ReadContext(tenant_ids=(world.tenant,), scopes=("internal",))
    found = await search(world.conn, world.schemas, ctx, "cafeteria", settings=DataSettings())
    assert [(h.title, set(h.arms)) for h in found.hits] == [("cafeteria soup", {"fts"})], (
        "full text answers with no notes and no embedder"
    )
    assert found.notes == [], "nothing failed: vectors were never asked for"


async def test_browse_and_get_page_a_source_and_hide_it_from_another_tenant(world: World) -> None:
    src = await world.source()
    await put_docs(world, src)
    mine = ReadContext(tenant_ids=(world.tenant,), scopes=("internal",))
    other = ReadContext(tenant_ids=(uuid.uuid4(),), scopes=("internal",))

    first = await reads.browse(world.conn, world.schemas, mine, src, limit=2)
    rest = await reads.browse(world.conn, world.schemas, mine, src, limit=2, after=first.next)
    assert (len(first.items), len(rest.items), rest.next) == (2, 1, None), "pages of two, then one"
    record = await reads.get(world.conn, world.schemas, mine, first.items[0].id)
    assert record is not None and record.body.startswith("body of"), "get returns the text"
    assert (
        await reads.read_file(world.conn, world.schemas, mine, world.env, first.items[0].id, 0)
        is None
    ), "a record with no file has none to read"
    assert await reads.get(world.conn, world.schemas, other, first.items[0].id) is None, (
        "another tenant cannot tell the record exists"
    )
    assert await reads.sources(world.conn, world.schemas, other) == [], "nor the source"


async def test_caller_sql_reads_views_only_and_cannot_change_role(world: World) -> None:
    src = await world.source()
    await put_docs(world, src)
    await world.sql(
        f"insert into {world.d}.datasets (source_id, name, title, scope) values (%s, 'docs', 'Docs', 'internal')",
        src,
    )
    await world.sql(
        f"insert into {world.d}.metrics (source_id, dataset, name, expr) values (%s, 'docs', 'n', 'count(*)')",
        src,
    )
    mine = ReadContext(tenant_ids=(world.tenant,), scopes=("internal",))
    other = ReadContext(tenant_ids=(uuid.uuid4(),), scopes=("internal",))

    async def ask(ctx: ReadContext, sql: str) -> list[dict[str, object]]:
        return (await data_sql(world.conn, world.schemas, ctx, sql)).rows

    titles = await ask(mine, "select title from records order by title")
    assert [r["title"] for r in titles] == [
        "cafeteria soup",
        "heron colony nests",
        "lake fish frogs",
    ], "the views answer under the reader's row policies"
    assert await ask(other, "select title from records") == [], (
        "another tenant's rows are not there"
    )
    assert [r["name"] for r in await ask(mine, "select name from datasets")] == ["docs"], (
        "datasets are views too"
    )
    listed = await reads.datasets(world.conn, world.schemas, mine)
    assert [(d.name, [m["name"] for m in d.metrics]) for d in listed] == [("docs", ["n"])], (
        "the catalog lists a dataset with its metrics"
    )

    for text in (
        f"select * from {world.d}.records",
        "select acl from records",
        "select 1; reset role",
        "select set_config('role', 'daimon_data_reader', false)",
        "select set_config('session_authorization', 'postgres', false)",
    ):
        with pytest.raises(SqlRefused):
            await ask(mine, text)
    who = await ask(mine, "select current_user as u")
    assert who == [{"u": "daimon_data_sql"}], (
        "after every attempt the text still runs as the sql role"
    )
    assert (await world.sql("select current_user as u, session_user as s"))[0]["u"] == (
        (await world.sql("select session_user as s"))[0]["s"]
    ), "and the connection's own role never moved"

    with pytest.raises(psycopg.Error):  # past the Python guard: the function itself refuses
        async with scoped(world.conn, world.schemas, mine):
            await world.conn.execute(
                f"select {world.d}.caller_sql(%s, 5)".encode(),
                ("select 1) t; reset role; select * from (select 1",),
            )
