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
from daimon.data import declared, identity, redaction
from daimon.data.identity import IdentityKey, Proof
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
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db, uuid.uuid4())


RULES = {
    "aliases": {"Acme": "client A", "Alice Smith": "an engineer"},
    "removals": ["zx9"],
    "regex": [{"pattern": r"asked", "replace": "wrote"}],
    "exclude_ids": ["doc:2"],
    "entities": [{"name": "Globex", "scope": "client:globex", "label": "[client]"}],
}
SOURCE = {"id": "feed", "kind": "feed", "name": "Feed", "redaction_set": "house"}


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
