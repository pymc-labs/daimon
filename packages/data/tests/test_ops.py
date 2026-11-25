"""Operations: the publish gate, the declared file, freshness and live sources."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest
from daimon.data import acl, declared, federated, identity, parity
from daimon.data.access import ReadContext, scoped
from daimon.data.identity import IdentityKey, Proof
from daimon.data.sync.connector import CONNECTORS, Connector, LiveHit, Source
from daimon.data.sync.engine import Env
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow


class Fake(Connector):
    """A connector with a settable verdict for `can_open` and a settable live behaviour."""

    kind = "fake"
    name = "Fake"
    default_scope = "internal"
    live = True
    verdict: bool | None = True
    live_mode = "ok"

    async def can_open(
        self, source: Source, external_id: str, subjects: Sequence[str]
    ) -> bool | None:
        return self.verdict

    async def live_search(self, source: Source, query: str, limit: int) -> list[LiveHit]:
        if self.live_mode == "error":
            raise RuntimeError("upstream down")
        if self.live_mode == "slow":
            await asyncio.sleep(5)
        return [LiveHit(ref="1", title=query)]


@pytest.fixture
def fake() -> Iterator[Fake]:
    connector = Fake()
    CONNECTORS["fake"] = connector
    yield connector
    del CONNECTORS["fake"]


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    data: DataDb
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.data.schemas.data

    @property
    def env(self) -> Env:
        return Env(self.data.schemas, ())

    async def sql(self, query: str, *params: object) -> list[DictRow]:
        cur = await self.conn.execute(query.encode(), params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall() if cur.description else []

    async def source(
        self, publication: str = "shadow", mode: str = "stored", **columns: object
    ) -> uuid.UUID:
        cols = {"config": '{"workspace": "w"}', **columns}
        rows = await self.sql(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, default_scope, mode,"
            f" publication, {', '.join(cols)}) values (%s, 'fake', 'src', 'fake', 'internal', %s,"
            f" %s, {', '.join(['%s'] * len(cols))}) returning id",
            self.tenant,
            mode,
            publication,
            *cols.values(),
        )
        return rows[0]["id"]


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db, uuid.uuid4())


async def test_publish_gate_blocks_on_wrong_more(store: Store, fake: Fake) -> None:
    """A source showing a person a record its own system hides is never published."""
    src = await store.source()
    me = uuid.uuid4()
    await identity.link(
        store.conn,
        store.data.schemas,
        tenant_id=store.tenant,
        account_id=me,
        proof=Proof(IdentityKey("fake", "w", "u1"), "oauth"),
    )
    list_id = await acl.intern(store.conn, store.data.schemas, src, [acl.ref("fake/w:user:u1")])
    await store.sql(
        f"insert into {store.d}.records (source_id, external_id, kind, occurred_at, scope)"
        " values (%s, 'doc:1', 'doc', now(), 'restricted:acl')",
        src,
    )
    await acl.set_records(
        store.conn,
        store.data.schemas,
        src,
        [{"ext": "doc:1", "acl": list_id, "until": None}],
        datetime.now(UTC),
    )

    async def compare() -> parity.Run:
        started = datetime.now(UTC)
        run = await parity.compare(store.conn, store.data.schemas, store.env, src)
        await parity.record(store.conn, store.data.schemas, src, run, 1, started)
        return run

    fake.verdict = False
    run = await compare()
    assert (run.wrong_more, run.wrong_less) == (1, 0), "shown here, hidden by the source"
    verdict = await parity.publish(store.conn, store.data.schemas, src)
    assert not verdict.met, "wrong-more refuses publishing"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await store.sql(
            f"update {store.d}.sources set publication = 'published' where id = %s", src
        )

    fake.verdict = True
    assert (await compare()).ok, "a clean run follows"
    assert (await parity.publish(store.conn, store.data.schemas, src)).met, "now it publishes"


async def test_declared_apply_never_deletes_and_refuses_a_lower_revision(
    store: Store, fake: Fake
) -> None:
    """A removed entry stays, a lower revision changes nothing, retiring keeps the row."""
    schemas = store.data.schemas

    def file(revision: int, **tenant: object) -> declared.Declaration:
        return declared.Declaration.model_validate(
            {"revision": revision, "tenants": [{"workspace": store.tenant, **tenant}]}
        )

    first = file(
        5,
        scopes=[{"id": "shared:eng", "kind": "shared", "name": "Eng"}],
        sources=[{"id": "wiki", "kind": "fake", "name": "Wiki", "mode": "federated"}],
    )
    report = await declared.apply(store.conn, schemas, store.env, first)
    assert not report.ignored and report.results, "the first file is applied"
    rows = await store.sql(
        f"select name, bound, publication from {store.d}.sources where tenant_id = %s", store.tenant
    )
    assert [(r["name"], r["bound"], r["publication"]) for r in rows] == [("Wiki", True, "shadow")]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await store.sql(
            f"update {store.d}.sources set name = 'x' where tenant_id = %s", store.tenant
        )

    lower = file(
        4, sources=[{"id": "wiki", "kind": "fake", "name": "Renamed", "mode": "federated"}]
    )
    assert (await declared.apply(store.conn, schemas, store.env, lower)).ignored, "refused"
    empty = file(6)
    await declared.apply(store.conn, schemas, store.env, empty)
    rows = await store.sql(f"select name from {store.d}.sources where tenant_id = %s", store.tenant)
    scopes = await store.sql(f"select id from {store.d}.scopes where tenant_id = %s", store.tenant)
    assert [r["name"] for r in rows] == ["Wiki"], "a lower revision changes nothing"
    assert len(scopes) == 1, "an entry removed from the file is left as it was"

    retire = file(
        7,
        sources=[
            {"id": "wiki", "kind": "fake", "name": "Wiki", "mode": "federated", "retire": True}
        ],
    )
    await declared.apply(store.conn, schemas, store.env, retire)
    rows = await store.sql(
        f"select publication from {store.d}.sources where tenant_id = %s", store.tenant
    )
    assert [r["publication"] for r in rows] == ["retired"], "retired, not deleted"


async def test_a_source_past_its_ceiling_is_hidden(store: Store) -> None:
    """Past its ceiling a source's records disappear from reads; a sync brings them back."""
    src = await store.source("published", fresh_ceiling_minutes=5)
    await store.sql(
        f"insert into {store.d}.records (source_id, external_id, kind, occurred_at, scope)"
        " values (%s, 'doc:1', 'doc', now(), 'internal')",
        src,
    )
    ctx = ReadContext((store.tenant,), scopes=("internal",))

    async def seen() -> int:
        async with scoped(store.conn, store.data.schemas, ctx) as c:
            rows = await (await c.execute(f"select id from {store.d}.records".encode())).fetchall()
        return len(rows)

    await store.sql(
        f"insert into {store.d}.source_cursors (source_id, last_sync_at, last_status)"
        " values (%s, now() - interval '10 minutes', 'ok')",
        src,
    )
    assert await seen() == 0, "its last sync is older than the ceiling"
    await store.sql(f"update {store.d}.source_cursors set last_sync_at = now()")
    assert await seen() == 1, "a fresh sync shows it again"


async def test_a_live_source_is_scope_gated_failing_open_and_stores_nothing(
    store: Store, fake: Fake
) -> None:
    """A live search reads upstream for callers holding the scope; failures skip the source."""
    src = await store.source("published", "federated")
    metered: list[tuple[uuid.UUID, int]] = []

    async def meter(source_id: uuid.UUID, hits: int) -> None:
        metered.append((source_id, hits))

    async def search(scopes: tuple[str, ...], seconds: float = 2.0) -> list[federated.LiveResult]:
        ctx = ReadContext((store.tenant,), scopes=scopes)
        return await federated.search(
            store.conn, store.data.schemas, store.env, ctx, "plan", seconds=seconds, meter=meter
        )

    (found,) = await search(("internal",))
    assert [h.title for h in found.hits] == ["plan"], "a caller with the scope gets hits"
    assert metered == [(src, 1)], "each live call is metered"
    assert await search(()) == [], "a caller without the scope reaches nothing"
    fake.live_mode = "error"
    assert [r.skipped for r in await search(("internal",))] == ["error"], "a failure fails open"
    fake.live_mode = "slow"
    assert [r.skipped for r in await search(("internal",), 0.05)] == ["timeout"], "so does a delay"
    stored = await store.sql(
        f"select count(*) as n from {store.d}.records where source_id = %s", src
    )
    assert stored[0]["n"] == 0, "nothing a live source returns is stored"
