"""Redaction rules: applied at ingest, hidden at read for guests and marked people, and a
changed pinned file stops its sources until approved."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from daimon.core.data_audience import DataAudience
from daimon.data import declared, identity, redaction
from daimon.data.access import ReadContext
from daimon.data.identity import IdentityKey, Proof
from daimon.data.reading import Reading, open_read
from daimon.data.sync.connector import CONNECTORS, Connector, ItemRecord, Record, Source, SyncState
from daimon.data.sync.engine import Env, run_source
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow

RECORDS = [
    ItemRecord("doc:1", "doc", "Plan", "Acme asked. The Alice Smith approved it, code zx9.",
               datetime.now(UTC), author="Alice Smith"),
    ItemRecord("doc:2", "doc", "Skipped", "never stored", datetime.now(UTC)),
]  # fmt: skip


class Feed(Connector):
    kind = "feed"
    name = "Feed"
    default_scope = "internal"

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        for r in RECORDS:
            yield r
        yield SyncState({}, {r.external_id for r in RECORDS})


@pytest.fixture
def feed() -> Iterator[None]:
    CONNECTORS["feed"] = Feed()
    yield
    del CONNECTORS["feed"]


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    data: DataDb
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.data.schemas.data

    async def sql(self, query: str, *params: object) -> list[DictRow]:
        cur = await self.conn.execute(query.encode(), params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall() if cur.description else []

    async def declare(self, revision: int, **sections: object) -> declared.Report:
        decl = declared.Declaration.model_validate(
            {"revision": revision, "tenants": [{"workspace": self.tenant, **sections}]}
        )
        return await declared.apply(self.conn, self.data.schemas, Env(self.data.schemas, ()), decl)

    async def sync(self) -> str:
        (src,) = await self.sql(
            f"select id from {self.d}.sources where tenant_id = %s", self.tenant
        )
        result = await run_source(self.conn, Env(self.data.schemas, ()), src["id"])
        return result.status

    async def texts(self) -> str:
        rows = await self.sql(
            f"select r.title || ' ' || r.body || ' ' || coalesce(r.author, '') || ' '"
            f" || coalesce(string_agg(c.text, ' '), '') as t from {self.d}.records r"
            f" left join {self.d}.chunks c on c.record_id = r.id"
            f" where r.source_id in (select id from {self.d}.sources where tenant_id = %s)"
            " group by r.id",
            self.tenant,
        )
        return " ".join(r["t"] for r in rows)


@pytest.fixture
async def store(data_db: DataDb, db_schema: str) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        tenant = uuid.uuid4()
        # The declared revision is one per store: each test starts with none applied.
        await conn.execute(f"delete from {data_db.schemas.data}.declared_state".encode())
        await conn.execute(f"set search_path = {db_schema}".encode())
        await conn.execute(
            b"insert into tenants (id, platform, external_id) values (%s, 'discord', %s)",
            (tenant, f"guild-{tenant.hex[:8]}"),
        )
        yield Store(conn, data_db, tenant)


RULES = {
    "aliases": {"Acme": "client A", "Alice Smith": "an engineer"},
    "removals": ["zx9"],
    "regex": [{"pattern": r"asked", "replace": "wrote"}],
    "exclude_ids": ["doc:2"],
    "entities": [{"name": "Globex", "scope": "client:globex", "label": "[client]"}],
}
SOURCE = {"id": "feed", "kind": "feed", "name": "Feed", "redaction_set": "house"}


@pytest.mark.parametrize(
    "pattern", [r"(\w+\s?)*", r"(a|aa)+", r"((a+))+", r"(.*a){20}", r"(?:ab)?", r"(a)\1"]
)
def test_a_pattern_that_can_run_forever_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError, match="can repeat|refer back"):
        redaction.parse(json.dumps({"regex": [{"pattern": pattern}]}))
    assert redaction.parse(json.dumps({"regex": [{"pattern": r"\d{3}-\w+\s?"}]})).regex


def test_a_set_applies_every_rule_at_storage_or_at_answer_time() -> None:
    """Unset keeps text rules at ingest and names at read; `storage` and `answer` move all of
    them to one time. `exclude_ids` is never stored either way."""
    text = "Acme asked Globex"
    unset = redaction.Rules.model_validate(RULES)
    storage, answer = (
        unset.model_copy(update={"at": "storage"}),
        unset.model_copy(update={"at": "answer"}),
    )
    assert redaction.Redactor(unset).text(text) == "client A wrote Globex", "as before"
    assert redaction.Redactor(storage).text(text) == "client A wrote [client]", "names too"
    assert redaction.Redactor(answer).text(text) == text, "answer sets store the source's text"
    for rules in (unset, storage, answer):
        assert redaction.Redactor(rules).excludes("doc:2"), "an excluded id is never stored"
    ctx = ReadContext((uuid.uuid4(),))
    own, other = uuid.uuid4(), uuid.uuid4()
    shown = Reading(ctx, None, {own: redaction.Redactor(answer, answer=True)})
    hits = [{"source_id": str(own), "t": text}, {"source_id": str(other), "t": text}]
    assert shown.show(hits) == [
        {"source_id": str(own), "t": "client A wrote Globex"},
        {"source_id": str(other), "t": text},
    ], "only its own source"
    assert shown.show(text, other) == text and shown.show({"t": text})["t"] != text
    assert shown.redacted and shown.names_hidden("Acme"), "sql, files and naming queries refused"
    assert not Reading(ctx, None).redacted, "no answer set: nothing refused"


async def test_changed_rules_apply_to_answers_until_their_source_syncs_again(
    store: Store, feed: None
) -> None:
    async def answered() -> bool:
        return bool(await redaction.answer_redactors(store.conn, store.data.schemas, store.tenant))

    await store.declare(1, redaction_sets=[{"id": "house", "rules": RULES}], sources=[SOURCE])
    assert await answered(), "nothing stored under the rules yet"
    assert await store.sync() == "ok" and not await answered(), "stored rewritten: ingest only"
    changed = {**RULES, "at": "storage"}
    await store.declare(2, redaction_sets=[{"id": "house", "rules": changed}], sources=[SOURCE])
    assert await answered(), "rows stored under the old rules are rewritten at answer time"
    assert await store.sync() == "ok" and not await answered()


async def test_rules_approved_during_a_sync_apply_to_answers_until_the_next_one(
    store: Store, feed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.declare(1, redaction_sets=[{"id": "house", "rules": RULES}], sources=[SOURCE])
    assert await store.sync() == "ok"
    fetch = Feed.fetch

    async def approved_meanwhile(self: Feed, source: Source) -> AsyncGenerator[Record]:
        await redaction.restart_sources(store.conn, store.data.schemas, store.tenant, "house")
        async for r in fetch(self, source):
            yield r

    monkeypatch.setattr(Feed, "fetch", approved_meanwhile)
    assert await store.sync() == "ok"
    assert await redaction.answer_redactors(store.conn, store.data.schemas, store.tenant), (
        "the run stored under the old rules"
    )
    monkeypatch.setattr(Feed, "fetch", fetch)
    assert await store.sync() == "ok"
    assert not await redaction.answer_redactors(store.conn, store.data.schemas, store.tenant)


async def test_removed_text_is_never_stored_and_excluded_records_are_dropped(
    store: Store, feed: None
) -> None:
    await store.declare(1, redaction_sets=[{"id": "house", "rules": RULES}], sources=[SOURCE])
    assert await store.sync() == "ok"
    text = await store.texts()
    for gone in ("Acme", "Alice", "Smith", "zx9", "asked", "never stored"):
        assert gone not in text, "the source's words are rewritten before storing and chunking"
    assert "client A wrote. An engineer approved it" in text, "an article moves with the alias"
    ids = await store.sql(
        f"select external_id from {store.d}.records where source_id in"
        f" (select id from {store.d}.sources where tenant_id = %s)",
        store.tenant,
    )
    assert [r["external_id"] for r in ids] == ["doc:1"], "an excluded id is never stored"


async def test_names_hide_from_a_guest_and_a_marked_person_only(store: Store) -> None:
    schemas = store.data.schemas
    await store.declare(1, redaction_sets=[{"id": "house", "rules": RULES}])
    admin, marked, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for account, subject in ((marked, "u1"), (other, "u2")):
        await identity.link(
            store.conn,
            schemas,
            tenant_id=store.tenant,
            account_id=account,
            proof=Proof(IdentityKey("chat", "w", subject), "oauth"),
        )
    await store.sql(
        f"insert into {store.d}.data_admins (tenant_id, account_id) values (%s, %s)",
        store.tenant,
        admin,
    )
    await redaction.set_redacted(store.conn, schemas, store.tenant, marked, True, by=admin)
    assert await redaction.redacted_among(store.conn, schemas, store.tenant, [marked, other]) == {
        marked
    }, "only the marked person"

    reader = await redaction.read_redactor(store.conn, schemas, store.tenant, ["internal"])
    assert reader({"text": "Globex renewed"}) == {"text": "[client] renewed"}, "a guest's view"
    holder = await redaction.read_redactor(store.conn, schemas, store.tenant, ["client:globex"])
    assert holder.text("Globex renewed") == "Globex renewed", "who holds the scope sees it"

    def audience(*people: uuid.UUID) -> DataAudience:
        return DataAudience(frozenset(people), frozenset(), False, True, False)

    async def reading(*people: uuid.UUID) -> Reading:
        return await open_read(store.conn, schemas, store.tenant, audience(*people), salt="t")

    seen = await reading(other)
    assert not seen.redacted and not seen.names_hidden("Globex"), "nothing hides from others"
    hidden = await reading(marked, other)
    assert hidden.redacted, "caller SQL and file bytes are refused while names hide"
    assert hidden.names_hidden("any news on globex?"), "a query naming an entity finds nothing"
    assert not hidden.names_hidden("any news?")
    assert hidden.show({"files": [{"name": "Globex deal.pdf"}]}) == {
        "files": [{"name": "[client] deal.pdf"}]
    }, "an attachment's name hides too"


def _file(rules: dict[str, object]) -> tuple[str, bytes]:
    data = json.dumps(rules).encode()
    return redaction.blob_sha(data), data


async def test_a_changed_pinned_file_stops_its_source_until_approved(
    store: Store, feed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas = store.data.schemas
    served = {"file": _file({"aliases": {"Acme": "client A"}})}

    def answer(request: httpx.Request) -> httpx.Response:
        sha, data = served["file"]
        return httpx.Response(
            200, json={"sha": sha, "encoding": "base64", "content": base64.b64encode(data).decode()}
        )

    monkeypatch.setattr(redaction, "transport", httpx.MockTransport(answer))
    pinned = {"id": "house", "repo": "org/rules", "path": "rules.json", "sha": served["file"][0]}
    await store.declare(1, redaction_sets=[pinned], sources=[SOURCE])
    assert await store.sync() == "skipped", "nothing is fetched yet"

    await redaction.refresh_all(store.conn, schemas)
    assert await store.sync() == "ok", "the declared sha is the approved one"
    assert "client A" in await store.texts()

    served["file"] = _file({"aliases": {"Acme": "client B"}})
    await redaction.refresh_all(store.conn, schemas)
    assert await store.sync() == "skipped", "a changed file waits for approval"
    (cursor,) = await store.sql(
        f"select c.last_error from {store.d}.source_cursors c join {store.d}.sources s"
        " on s.id = c.source_id where s.tenant_id = %s",
        store.tenant,
    )
    assert "await approval" in cursor["last_error"]

    admin = uuid.uuid4()
    await store.sql(
        f"insert into {store.d}.data_admins (tenant_id, account_id) values (%s, %s)",
        store.tenant,
        admin,
    )
    detail = await redaction.show_set(store.conn, schemas, store.tenant, "house", by=admin)
    assert detail.info.pending and detail.pending_rules is not None
    await redaction.approve(store.conn, schemas, store.tenant, "house", served["file"][0], by=admin)
    assert await store.sync() == "ok", "approved: the source syncs with the new rules"
    assert "client B" in await store.texts()
