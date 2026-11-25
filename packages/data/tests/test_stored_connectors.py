"""The Notion, Bluedot and LinkedIn connectors against fake servers: who reads what, what a
complete run removes, and what a refused read leaves alone."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.schema import Schemas
from daimon.data.sync import connectors  # noqa: F401  # pyright: ignore[reportUnusedImport]
from daimon.data.sync.connector import CONNECTORS, Json, Person, Source
from daimon.data.sync.connectors.bluedot import BluedotConnector
from daimon.data.sync.connectors.linkedin import LinkedInConnector
from daimon.data.sync.connectors.notion import NotionConnector
from daimon.data.sync.credentials import seal
from daimon.data.sync.engine import Env, RunResult, run_source
from daimon.data.sync.rate import RateBudget
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def add(self, kind: str, credential: Json, **config: object) -> uuid.UUID:
        cur = await self.conn.execute(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, config, credential,"
            " default_scope, publication) values (%s, %s, %s, %s, %s::jsonb, %s,"
            " 'internal', 'published') returning id".encode(),
            (self.tenant, kind, kind, kind, Jsonb(config), seal((KEY,), credential)),
        )
        row = await cur.fetchone()
        assert row
        return row["id"]

    async def connect(self, src: uuid.UUID, subject_: str, status: str = "active") -> uuid.UUID:
        """A person signed in at the source, with a token that does not need refreshing."""
        account = uuid.uuid4()
        await self.conn.execute(
            f"insert into {self.d}.person_connections (tenant_id, account_id, source_id, provider,"
            " workspace, subject, credential, status, access_expires_at)"
            " select %s, %s, id, kind, 'ws', %s, %s, %s, now() + interval '1 hour'"
            f" from {self.d}.sources where id = %s".encode(),
            (
                self.tenant,
                account,
                subject_,
                seal((KEY,), {"access_token": f"tok-{subject_}"}),
                status,
                src,
            ),
        )
        return account

    async def run(self, src: uuid.UUID) -> RunResult:
        return await run_source(self.conn, self.env, src)

    async def ok(self, src: uuid.UUID) -> RunResult:
        result = await self.run(src)
        assert result.status == "ok", result.error
        return result

    async def stored(self, src: uuid.UUID) -> set[str]:
        cur = await self.conn.execute(
            f"select external_id from {self.d}.records where source_id = %s".encode(), (src,)
        )
        return {r["external_id"] for r in await cur.fetchall()}

    async def sees(self, provider: str, workspace: str, user: str) -> set[str]:
        ctx = ReadContext((self.tenant,), readers=(subject(provider, workspace, "user", user),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            cur = await c.execute(f"select external_id from {self.d}.records".encode())
            return {r["external_id"] for r in await cur.fetchall()}


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, Env(data_db.schemas, (KEY,)), uuid.uuid4())


# ---- Notion ----------------------------------------------------------------------------------

A, B, X, Y = (f"0000000{n}-0000-0000-0000-000000000000" for n in (1, 2, 3, 4))


def page(
    id_: str, title: str, parent: str | None = None, edited: str = "2026-01-02T03:04:00Z"
) -> Json:
    where = {"type": "page_id", "page_id": parent} if parent else {"type": "workspace"}
    return {
        "object": "page",
        "id": id_,
        "parent": where,
        "last_edited_time": edited,
        "url": f"https://www.notion.so/{id_}",
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
    }


class FakeNotion:
    """Notion's REST API and its MCP server, and a record of what was asked."""

    def __init__(self) -> None:
        self.pages: dict[str, Json] = {}
        self.text: dict[str, str] = {}
        self.valid = {"tok-u-alice", "tok-u-bob"}
        self.opens: dict[str, set[str]] = {}
        """Token -> the pages that person's own session fetches."""
        self.people = ["u-alice"]
        self.users_refused = False
        self.refuse: set[str] = set()
        """Pages whose blocks answer not-found."""
        self.broken = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "mcp.notion.com":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            ok = token in self.valid
            msg = json.loads(request.content)
            if msg.get("method") == "tools/call":
                page_id = msg["params"]["arguments"]["id"]
                if page_id not in self.opens.get(token, set()):
                    text, error = json.dumps({"code": "object_not_found"}), True
                else:
                    text, error = f"<page>{page_id}</page>", False
                result = {"isError": error, "content": [{"type": "text", "text": text}]}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": result})
            return httpx.Response(200 if ok else 401, headers={"mcp-session-id": "s"}, json={})
        if self.broken and path.endswith("/search"):
            return httpx.Response(500)
        if path.endswith("/users/me"):
            return httpx.Response(200, json={"id": "bot"})
        if path.endswith("/users"):
            if self.users_refused:
                return httpx.Response(403, json={"code": "restricted_resource"})
            found: list[Json] = [
                {
                    "id": u,
                    "type": "person",
                    "name": u,
                    "person": {"email": f"{u}@example.com"},
                }
                for u in self.people
            ]
            return httpx.Response(200, json={"results": found, "has_more": False})
        if path.endswith("/search"):
            return httpx.Response(
                200, json={"results": list(self.pages.values()), "has_more": False}
            )
        if "/blocks/" in path and path.endswith("/children"):
            pid = path.split("/blocks/")[1].split("/")[0]
            if pid in self.refuse:
                return httpx.Response(404, json={"code": "object_not_found"})
            blocks = [
                {
                    "id": f"b-{pid}",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": self.text.get(pid, "")}]},
                }
            ]
            return httpx.Response(200, json={"results": blocks, "has_more": False})
        return httpx.Response(404)


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    fake = FakeNotion()
    fake.pages = {
        A: page(A, "Handbook"),
        B: page(B, "Onboarding", A),
        X: page(X, "Salaries"),
        Y: page(Y, "Bands", X),
    }
    fake.text = {A: "alpha", B: "beta", X: "pay", Y: "bands"}
    monkeypatch.setitem(CONNECTORS, "notion", NotionConnector(httpx.MockTransport(fake), 0))
    return fake


async def test_a_notion_page_is_read_by_signed_in_people_and_an_excluded_tree_by_nobody(
    store: Store, notion: FakeNotion
) -> None:
    src = await store.add("notion", {"token": "secret"}, workspace="ws", exclude=[X])
    await store.connect(src, "u-alice")
    await store.ok(src)

    assert await store.stored(src) == {f"page:{i}" for i in (A, B, X, Y)}
    assert await store.sees("notion", "ws", "u-alice") == {f"page:{A}", f"page:{B}"}, (
        "a signed-in person reads the pages, bar the excluded tree"
    )
    assert await store.sees("notion", "ws", "u-bob") == set(), "nobody who has not signed in"

    notion.valid.clear()
    await store.ok(src)
    assert await store.sees("notion", "ws", "u-alice") == {f"page:{A}", f"page:{B}"}, (
        "a token Notion refuses publishes nothing: the earlier unit lives out its lease"
    )
    await store.conn.execute(
        f"update {store.d}.person_connections set status = 'reconnect' where source_id = %s".encode(),
        (src,),
    )
    await store.ok(src)
    assert await store.sees("notion", "ws", "u-alice") == set(), (
        "a sign-in that stopped proving itself ends the person's access at once"
    )


async def test_a_notion_sign_in_outside_the_integrations_users_reads_nothing(
    store: Store, notion: FakeNotion
) -> None:
    src = await store.add("notion", {"token": "secret"}, workspace="ws")
    await store.connect(src, "u-alice")
    await store.connect(src, "u-mallory")
    notion.valid.add("tok-u-mallory")
    await store.ok(src)
    assert await store.sees("notion", "ws", "u-alice") == {f"page:{i}" for i in (A, B, X, Y)}
    assert await store.sees("notion", "ws", "u-mallory") == set(), (
        "a Notion account the integration does not list is not the workspace's"
    )

    notion.users_refused = True
    await store.ok(src)  # a refused user list is no roster, not a failed run
    assert await store.sees("notion", "ws", "u-alice") == set(), "with no roster nobody reads"


async def test_a_notion_page_opens_only_through_the_readers_own_session(
    notion: FakeNotion,
) -> None:
    connector = CONNECTORS["notion"]
    assert connector.recheck_reads and connector.has_can_open()
    people = tuple(Person(uuid.uuid4(), u, f"tok-{u}") for u in ("u-alice", "u-bob"))
    source = Source(
        uuid.uuid4(), uuid.uuid4(), "notion", "n", "notion", {"workspace": "ws"}, {}, "internal",
        {}, None, RateBudget(0), people=people,
    )  # fmt: skip
    notion.valid.add("tok-u-bob")
    notion.opens = {"tok-u-alice": {A}}

    assert await connector.can_open(source, f"page:{A}", ["u-alice"]) is True
    assert await connector.can_open(source, f"page:{A}", ["u-bob"]) is False, (
        "bob's own fetch fails"
    )
    assert await connector.can_open(source, f"page:{A}", ["u-nobody"]) is None, "not signed in"


async def test_a_notion_page_that_vanishes_from_a_complete_listing_is_removed_and_a_refusal_keeps_it(
    store: Store, notion: FakeNotion
) -> None:
    src = await store.add("notion", {"token": "secret"}, workspace="ws")
    await store.ok(src)

    notion.pages[A] = page(A, "Handbook", edited="2026-02-02T03:04:00Z")
    notion.refuse.add(A)
    del notion.pages[B]
    await store.ok(src)
    assert await store.stored(src) == {f"page:{i}" for i in (A, X, Y)}, (
        "the page gone from the listing is removed; the one whose text was refused stays"
    )

    notion.broken = True
    assert (await store.run(src)).status == "error"
    assert await store.stored(src) == {f"page:{i}" for i in (A, X, Y)}, (
        "a failed listing removes nothing"
    )


# ---- Bluedot ---------------------------------------------------------------------------------


class FakeBluedot:
    """Bluedot's MCP server: each token is one person with their own lists."""

    def __init__(self) -> None:
        self.lists: dict[str, dict[str, Json]] = {}
        """subject -> {meeting id: listing item}"""
        self.collections: dict[str, list[str]] = {}
        self.details: dict[str, Json] = {}
        self.refused: set[str] = set()
        self.broken: set[str] = set()
        self.reads: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        who = request.headers["authorization"].removeprefix("Bearer tok-")
        msg = json.loads(request.content)
        headers = {"mcp-session-id": "s"}
        if "id" not in msg:
            return httpx.Response(202, headers=headers)
        if msg["method"] == "initialize":
            return httpx.Response(
                200, headers=headers, json={"jsonrpc": "2.0", "id": 1, "result": {}}
            )
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        if who in self.broken and name == "list_meetings":
            return httpx.Response(500)
        out: object
        match name:
            case "get_current_user":
                out = {"id": who}
            case "get_workspace":
                out = {"participants": [{"userId": "u-alice", "email": "alice@example.com"}]}
            case "list_collections":
                out = {"collections": [{"id": c} for c in self.collections.get(who, [])]}
            case "list_meetings":
                out = {
                    "items": list(self.lists.get(who, {}).values()),
                    "pagination": {"hasNext": False},
                }
            case _:
                mid = str(args["videoId"])
                if mid in self.refused:
                    return self._result(
                        msg, {"content": [{"type": "text", "text": "not found"}], "isError": True}
                    )
                self.reads.append(mid)
                out = self.details[mid]
        return self._result(msg, {"structuredContent": out})

    @staticmethod
    def _result(msg: Json, result: Json) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"mcp-session-id": "s"},
            json={"jsonrpc": "2.0", "id": msg["id"], "result": result},
        )


def meeting(mid: str, collection: str | None = None, tenancy: str = "workspace") -> Json:
    return {"id": mid, "collectionId": collection, "tenancy": tenancy}


@pytest.fixture
def bluedot(monkeypatch: pytest.MonkeyPatch) -> FakeBluedot:
    fake = FakeBluedot()
    fake.collections = {"u-alice": ["c1"]}
    fake.lists = {
        "u-alice": {"m1": meeting("m1", "c1"), "m2": meeting("m2", None, "user")},
        "u-bob": {"m2": meeting("m2", None, "user")},
    }
    fake.details = {
        "m1": {"id": "m1", "title": "Weekly", "summary": "ship it", "transcript": "A: hi"},
        "m2": {
            "id": "m2",
            "title": "One to one",
            "summary": "private",
            "transcript": "A: hi",
            "uploadedBy": {"email": "alice@example.com"},
            "sharedWithUsers": [{"email": "bob@example.com"}],
        },
    }
    monkeypatch.setitem(CONNECTORS, "bluedot", BluedotConnector(httpx.MockTransport(fake)))
    return fake


async def test_a_bluedot_meeting_is_read_by_the_people_who_list_it(
    store: Store, bluedot: FakeBluedot
) -> None:
    src = await store.add("bluedot", {}, workspace="ws")
    await store.connect(src, "u-alice")
    await store.connect(src, "u-bob")
    await store.ok(src)

    assert await store.stored(src) == {"meeting:m1", "meeting:m2"}
    assert await store.sees("bluedot", "ws", "u-alice") == {"meeting:m1", "meeting:m2"}
    assert await store.sees("bluedot", "ws", "u-bob") == {"meeting:m2"}, (
        "a person who lists no collection reads only the private meeting they are on"
    )
    assert await store.sees("bluedot", "ws", "u-carol") == set()

    await store.ok(src)
    assert bluedot.reads.count("m1") == 1, "a shared meeting that has not changed is read once"


async def test_a_bluedot_meeting_goes_only_when_every_list_is_complete_and_a_refusal_keeps_the_rest(
    store: Store, bluedot: FakeBluedot
) -> None:
    src = await store.add("bluedot", {}, workspace="ws")
    await store.connect(src, "u-alice")
    await store.connect(src, "u-bob")
    await store.ok(src)

    del bluedot.lists["u-alice"]["m1"]
    bluedot.broken.add("u-bob")
    await store.ok(src)
    assert await store.stored(src) == {"meeting:m1", "meeting:m2"}, (
        "a person whose list failed makes the run incomplete: nothing is removed"
    )

    bluedot.broken.clear()
    bluedot.lists["u-alice"]["m3"] = meeting("m3", "c1")
    bluedot.details["m3"] = {"id": "m3", "title": "New", "summary": "s", "transcript": "t"}
    bluedot.refused.add("m3")
    await store.ok(src)
    assert await store.stored(src) == {"meeting:m2"}, (
        "complete lists remove the meeting nobody lists; a meeting Bluedot refuses is not stored"
    )


# ---- LinkedIn --------------------------------------------------------------------------------


def post(id_: str, text: str, **extra: object) -> Json:
    at = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
    return {
        "id": id_,
        "commentary": text,
        "visibility": "PUBLIC",
        "lifecycleState": "PUBLISHED",
        "publishedAt": at,
        **extra,
    }


async def test_only_public_published_posts_are_stored_and_an_empty_answer_removes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = [
        post("urn:li:share:1", "hello \\(world\\)"),
        post("urn:li:share:2", "friends only", visibility="CONNECTIONS"),
        post("urn:li:share:3", "an ad", isDsc=True),
    ]

    def fake(request: httpx.Request) -> httpx.Response:
        assert request.url.params["author"] == "urn:li:organization:123"
        return httpx.Response(200, json={"elements": posts, "paging": {"total": len(posts)}})

    monkeypatch.setitem(CONNECTORS, "linkedin", LinkedInConnector(httpx.MockTransport(fake)))
    src = await store.add("linkedin", {"token": "t"}, organization="123")
    await store.ok(src)
    assert await store.stored(src) == {"post:urn:li:share:1"}, "only the public post"

    posts[:] = [
        post("urn:li:share:4", "second"),
        post("urn:li:share:2", "x", visibility="CONNECTIONS"),
    ]
    await store.ok(src)
    assert await store.stored(src) == {"post:urn:li:share:4"}, "a post no longer listed goes"

    posts.clear()
    assert (await store.run(src)).status == "error"
    assert await store.stored(src) == {"post:urn:li:share:4"}, "an empty answer removes nothing"
