"""Operations: the publish gate, the declared file, freshness, re-checks and live sources."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest
from cryptography.fernet import Fernet
from daimon.data import (
    acl,
    caller_sql,
    declared,
    federated,
    identity,
    manage,
    parity,
    reads,
    redaction,
)
from daimon.data.access import ReadContext
from daimon.data.identity import IdentityKey, Proof
from daimon.data.search import search
from daimon.data.sync.connector import CONNECTORS, Connector, LiveHit, Source
from daimon.data.sync.credentials import unseal
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

    async def fetch_file(
        self, source: Source, payload: object, message: str, name: str, index: int = 0
    ) -> tuple[bytes, str] | None:
        return (b"Acme wrote", "text/plain") if name.endswith(".txt") else (b"Acme", "image/png")


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

    async def record(self, source_id: uuid.UUID, text: str = "the launch plan") -> uuid.UUID:
        """A stored record of `source_id` in scope `internal`, with one chunk of `text`."""
        rows = await self.sql(
            f"insert into {self.d}.records (source_id, external_id, kind, occurred_at, scope)"
            " values (%s, 'doc:1', 'doc', now(), 'internal') returning id",
            source_id,
        )
        await self.sql(
            f"insert into {self.d}.chunks (record_id, source_id, ord, text, scope)"
            " values (%s, %s, 0, %s, 'internal')",
            rows[0]["id"],
            source_id,
            text,
        )
        return rows[0]["id"]

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
        # The declared revision is one per store: each test starts with none applied.
        await conn.execute(f"delete from {data_db.schemas.data}.declared_state".encode())
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
    store: Store, fake: Fake, db_schema: str
) -> None:
    """A removed entry stays, a lower revision changes nothing, retiring keeps the row."""
    schemas = store.data.schemas
    await store.sql(f"set search_path = {db_schema}")
    await store.sql(
        "insert into tenants (id, platform, external_id) values (%s, 'discord', %s)",
        store.tenant,
        f"guild-{uuid.uuid4().hex[:8]}",
    )

    def file(revision: int, **tenant: object) -> declared.Declaration:
        return declared.Declaration.model_validate(
            {"revision": revision, "tenants": [{"workspace": store.tenant, **tenant}]}
        )

    first = file(
        5,
        scopes=[{"id": "shared:eng", "kind": "shared", "name": "Eng"}],
        sources=[
            {"id": "wiki", "kind": "fake", "name": "Wiki", "mode": "federated", "group": "google"}
        ],
    )
    report = await declared.apply(store.conn, schemas, store.env, first)
    assert not report.ignored and report.results, "the first file is applied"
    rows = await store.sql(
        f"select name, bound, publication, source_group from {store.d}.sources"
        " where tenant_id = %s",
        store.tenant,
    )
    assert [(r["name"], r["bound"], r["publication"], r["source_group"]) for r in rows] == [
        ("Wiki", True, "shadow", "google")
    ], "the declared group is the source's own column"
    for edit in ("name = 'x'", "source_group = 'other'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await store.sql(
                f"update {store.d}.sources set {edit} where tenant_id = %s", store.tenant
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
    """Past its ceiling a source's records disappear from search; a sync brings them back."""
    src = await store.source("published", fresh_ceiling_minutes=5)
    await store.record(src)
    ctx = ReadContext((store.tenant,), scopes=("internal",))

    async def seen() -> int:
        found = await search(
            store.conn, store.data.schemas, ctx, "launch plan", settings=store.data.settings,
            use_vectors=False, use_rerank=False,
        )  # fmt: skip
        return len(found.hits)

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


async def test_a_record_the_source_now_refuses_is_not_shown(
    store: Store, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that re-checks reads is asked at read time; its refusal hides the record.
    Caller SQL cannot be re-checked, so it never sees such a source."""
    monkeypatch.setattr(Fake, "recheck_reads", True)
    src = await store.source("published")
    record = await store.record(src)
    ctx = ReadContext((store.tenant,), scopes=("internal",), readers=("fake/w:user:alice",))
    schemas = store.data.schemas

    assert await reads.get(store.conn, schemas, ctx, record, store.env), "the source opens it"
    page = await reads.browse(store.conn, schemas, ctx, src, env=store.env)
    assert len(page.items) == 1, "browse lists what the source opens"
    sql = await caller_sql.data_sql(store.conn, schemas, ctx, "select id from records")
    assert sql.rows == [], "caller SQL leaves out a source that re-checks reads"
    fake.verdict = False
    assert await reads.get(store.conn, schemas, ctx, record, store.env) is None, "now refused"
    page = await reads.browse(store.conn, schemas, ctx, src, env=store.env)
    assert page.items == [], "browse drops it too"
    found = await search(
        store.conn, schemas, ctx, "launch plan", settings=store.data.settings,
        use_vectors=False, use_rerank=False, env=store.env,
    )  # fmt: skip
    assert found.hits == [], "search drops it too"


async def test_a_text_file_goes_through_its_sources_answer_rules(store: Store, fake: Fake) -> None:
    """Answer-time rules reach file text, best effort; other bytes pass as the source sent them."""
    src = await store.source("published")
    record = await store.record(src)
    for ord_, name in enumerate(("notes.txt", "logo.png")):
        await store.sql(
            f"insert into {store.d}.attachments (source_id, external_id, ord, name)"
            " values (%s, 'doc:1', %s, %s)",
            src, ord_, name,
        )  # fmt: skip
    ctx = ReadContext((store.tenant,), scopes=("internal",))
    rules = redaction.Rules.model_validate({"aliases": {"Acme": "client A"}, "at": "answer"})
    answer = {src: redaction.Redactor(rules, answer=True)}
    read = [
        await reads.read_file(store.conn, store.data.schemas, ctx, store.env, record, i, answer)
        for i in (0, 1)
    ]
    assert read == [(b"client A wrote", "text/plain"), (b"Acme", "image/png")]


async def test_a_source_made_for_a_private_form_waits_paused_for_its_credential(
    store: Store,
) -> None:
    """Made without its secret it is paused; the form's value seals onto it and resumes it once.
    A later value replaces the credential and leaves the state alone."""
    keys, schemas, by = (Fernet.generate_key().decode(),), store.data.schemas, uuid.uuid4()
    src = await manage.create_source(
        store.conn, schemas, keys, tenant_id=store.tenant, by=by, kind="postgres", name="Orders",
        settings={"view": "public.docs"}, credential_later=True,
    )  # fmt: skip

    async def state() -> tuple[str, dict[str, str]]:
        row = (
            await store.sql(f"select status, credential from {store.d}.sources where id = %s", src)
        )[0]
        return row["status"], unseal(keys, row["credential"])

    assert await state() == ("paused", {})

    async def put(value: str) -> None:
        await manage.set_credential(
            store.conn, schemas, keys, tenant_id=store.tenant, by=by, source_id=src, value=value
        )

    await put("postgresql://a@db/app")
    assert await state() == ("active", {"dsn": "postgresql://a@db/app"}), "its first one resumes it"
    await manage.act(
        store.conn, schemas, tenant_id=store.tenant, by=by, source_id=src, action="pause"
    )
    await put("postgresql://b@db/app")
    assert await state() == ("paused", {"dsn": "postgresql://b@db/app"}), "a paused source stays so"
    with pytest.raises(identity.Missing):
        await manage.set_credential(
            store.conn, schemas, keys, tenant_id=uuid.uuid4(), by=by, source_id=src, value="x"
        )


async def test_a_live_answer_in_search_is_recorded_as_the_askers(store: Store, fake: Fake) -> None:
    """A search asks the caller's live sources and records each call under the asker."""
    src = await store.source("published", "federated")
    asker = uuid.uuid4()
    found = await search(
        store.conn, store.data.schemas, ReadContext((store.tenant,), scopes=("internal",)),
        "plan", settings=store.data.settings, use_vectors=False, use_rerank=False,
        env=store.env,
        live_meter=federated.audit_meter(store.conn, store.data.schemas, store.tenant, asker),
    )  # fmt: skip
    assert [h.title for r in found.live for h in r.hits] == ["plan"], "the live source answered"
    rows = await store.sql(
        f"select account_id, kind, detail from {store.d}.audit where source_id = %s", src
    )
    assert [(r["account_id"], r["kind"], r["detail"]) for r in rows] == [
        (asker, "source.live_search", {"hits": 1})
    ]


async def test_a_declared_workspace_may_be_named_by_its_platform_id(
    store: Store, db_schema: str
) -> None:
    """`<platform>:<id>` resolves to the workspace here; an unknown one is skipped."""
    external = f"guild-{uuid.uuid4().hex[:8]}"
    await store.sql(f"set search_path = {db_schema}")
    await store.sql(
        "insert into tenants (id, platform, external_id) values (%s, 'discord', %s)",
        store.tenant,
        external,
    )
    decl = declared.Declaration.model_validate({
        "revision": 1000,
        "tenants": [
            {"workspace": f"discord:{external}",
             "scopes": [{"id": "shared:ops", "kind": "shared", "name": "Ops"}]},
            {"workspace": "discord:nowhere", "scopes": []},
        ],
    })  # fmt: skip
    report = await declared.apply(store.conn, store.data.schemas, store.env, decl)
    rows = await store.sql(f"select id from {store.d}.scopes where tenant_id = %s", store.tenant)
    assert [r["id"] for r in rows] == ["shared:ops"], "the scope lands in the named workspace"
    assert {"section": "workspace", "id": "", "action": "error", "error": "no such workspace"} in (
        report.results or []
    )
