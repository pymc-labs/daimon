"""The Toggl, Linear and HubSpot connectors against fake servers: who reads a stored time entry,
who a live one, a live source answering only a holder of its scope, and a refused read."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.data import federated, identity
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.identity import IdentityKey, Proof
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, Json
from daimon.data.sync.connectors import toggl
from daimon.data.sync.credentials import seal
from daimon.data.sync.engine import Env, run_source
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()
WS = "4242"
NOW = datetime.now(UTC)
ADMIN, LEAD, MEMBER, GONE = 1, 2, 3, 4


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def add(
        self, kind: str, scope: str, mode: str, config: Json, credential: Json | None = None
    ) -> uuid.UUID:
        cur = await self.conn.execute(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, config, credential,"
            " default_scope, mode, publication) values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s,"
            " 'published') returning id".encode(),
            (
                self.tenant,
                kind,
                kind,
                kind,
                Jsonb(config),
                seal((KEY,), credential or {"token": "t"}),
                scope,
                mode,
            ),
        )
        row = await cur.fetchone()
        assert row
        return row["id"]

    async def ok(self, src: uuid.UUID) -> None:
        result = await run_source(self.conn, self.env, src)
        assert result.status == "ok", result.error

    async def stored(self, src: uuid.UUID) -> set[str]:
        cur = await self.conn.execute(
            f"select external_id from {self.d}.records where source_id = %s".encode(), (src,)
        )
        return {r["external_id"] for r in await cur.fetchall()}

    async def sees(self, user: int) -> set[str]:
        ctx = ReadContext((self.tenant,), readers=(subject("toggl", WS, "user", str(user)),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            cur = await c.execute(f"select external_id from {self.d}.records".encode())
            return {r["external_id"] for r in await cur.fetchall()}

    async def link(self, provider: str, workspace: str, person: str) -> str:
        account = uuid.uuid4()
        await identity.link(
            self.conn,
            self.schemas,
            tenant_id=self.tenant,
            account_id=account,
            proof=Proof(IdentityKey(provider, workspace, person), "oauth"),
        )
        return f"account:{account}"

    async def live(self, scopes: tuple[str, ...], query: str, *readers: str) -> list[str]:
        ctx = ReadContext((self.tenant,), scopes=scopes, readers=readers)
        found = await federated.search(self.conn, self.schemas, self.env, ctx, query, seconds=5)
        return [h.title for r in found for h in r.hits]


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, Env(data_db.schemas, (KEY,)), uuid.uuid4())


# ---- Toggl ----


class FakeToggl:
    """A workspace of four people (an admin, a member who manages project 10, a member, and a
    departed one) and three entries; it answers GET, and POST only to the report search."""

    def __init__(self) -> None:
        day = (NOW - timedelta(days=2)).date().isoformat()
        self.start = f"{day}T09:00:00+00:00"
        self.users = [
            {"uid": ADMIN, "name": "Ada", "email": "a@example.com", "role_id": 1, "active": True},
            {"uid": LEAD, "name": "Lee", "email": "l@example.com", "role_id": 3, "active": True},
            {"uid": MEMBER, "name": "Max", "email": "m@example.com", "role_id": 3, "active": True},
            {"uid": GONE, "name": "Gus", "email": "g@example.com", "role_id": 3, "active": False},
        ]
        self.projects = {10: "Alpha", 11: "Beta"}
        self.entries = {1: (MEMBER, 11), 2: (LEAD, 11), 3: (MEMBER, 10), 4: (GONE, 11)}
        self.seen: list[tuple[str, str]] = []

    def entry(self, i: int) -> Json:
        user, project = self.entries[i]
        who = next(u["name"] for u in self.users if u["uid"] == user)
        return {
            "user_id": user,
            "username": who,
            "project_id": project,
            "description": f"work item {i}",
            "billable": True,
            "rate": 99,
            "time_entries": [
                {
                    "id": i,
                    "start": self.start,
                    "stop": self.start,
                    "seconds": 3600,
                    "at": self.start,
                }
            ],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append((request.method, path))
        if request.method == "POST":
            ok = path == f"/reports/api/v3/workspace/{WS}/search/time_entries"
            return (
                httpx.Response(200, json=[self.entry(i) for i in self.entries])
                if ok
                else httpx.Response(405)
            )
        base = f"/api/v9/workspaces/{WS}"
        match path.removeprefix(base):
            case "/workspace_users":
                return httpx.Response(200, json=self.users)
            case "/project_users":
                return httpx.Response(
                    200, json=[{"project_id": 10, "user_id": LEAD, "manager": True}]
                )
            case "/projects":
                return httpx.Response(
                    200,
                    json=[
                        {"id": i, "name": n, "is_private": True} for i, n in self.projects.items()
                    ],
                )
            case "/clients":
                return httpx.Response(200, json=[])
            case "":
                return httpx.Response(200, json={"id": int(WS)})
            case _:
                return httpx.Response(404)


@pytest.fixture
def fake_toggl(monkeypatch: pytest.MonkeyPatch) -> FakeToggl:
    fake = FakeToggl()
    monkeypatch.setattr(CONNECTORS["toggl"], "transport", httpx.MockTransport(fake))
    return fake


def day_of(fake: FakeToggl, i: int) -> str:
    return f"time_entry:{fake.start[:10]}:{i}"


async def test_toggl_entries_read_by_role_author_and_project_manager(
    store: Store, fake_toggl: FakeToggl, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"workspace": WS, "history_from": (NOW - timedelta(days=10)).date().isoformat()}
    src = await store.add("toggl", "restricted:acl", "stored", config)
    await store.ok(src)

    entries = {day_of(fake_toggl, i) for i in (1, 2, 3, 4)}
    assert await store.stored(src) == entries | {"project:10", "project:11"}
    meta = {"project:10", "project:11"}
    assert await store.sees(ADMIN) == entries | meta, "an admin reads everything"
    assert await store.sees(LEAD) == {day_of(fake_toggl, 2), day_of(fake_toggl, 3)}, (
        "a member reads their own and those of the project they manage, no project records"
    )
    assert await store.sees(MEMBER) == {day_of(fake_toggl, 1), day_of(fake_toggl, 3)}
    assert await store.sees(GONE) == set(), "a departed account reads nothing"
    assert ("GET", "/api/v9/me") not in fake_toggl.seen
    assert all(m == "GET" or "search/time_entries" in p for m, p in fake_toggl.seen)
    cur = await store.conn.execute(
        f"select payload::text || body as p from {store.d}.records where source_id = %s".encode(),
        (src,),
    )
    assert "99" not in "".join(r["p"] for r in await cur.fetchall()), "no rate is kept"

    del fake_toggl.entries[3]
    del fake_toggl.projects[11]
    await store.ok(src)
    assert day_of(fake_toggl, 3) in await store.stored(src), "a window removes no entry"
    assert "project:11" not in await store.stored(src), "a project gone from the list goes"

    later = NOW + timedelta(days=2)
    monkeypatch.setattr(toggl, "now", lambda: later)
    await store.ok(src)
    assert day_of(fake_toggl, 3) not in await store.stored(src), "a daily pass removes it"
    assert day_of(fake_toggl, 1) in await store.stored(src)


async def test_toggl_live_shows_each_person_what_toggl_shows_them(
    store: Store, fake_toggl: FakeToggl
) -> None:
    await store.add("toggl", "internal", "federated", {"workspace": WS})
    admin = await store.link("toggl", WS, str(ADMIN))
    lead = await store.link("toggl", WS, str(LEAD))
    member = await store.link("toggl", WS, str(MEMBER))
    stranger = f"account:{uuid.uuid4()}"
    scopes = ("internal",)

    assert len(await store.live(scopes, "work", admin)) == 4, "an admin reads every entry"
    assert await store.live(scopes, "item 1", admin) == ["work item 1"], "every word must match"
    assert await store.live(scopes, "item", lead) == ["work item 2", "work item 3"]
    assert await store.live(scopes, "item", member) == ["work item 1", "work item 3"]
    assert await store.live(scopes, "item", stranger) == [], "an unlinked person sees nothing"
    assert await store.live(scopes, "item", lead, member) == ["work item 3"], "what both may read"
    assert await store.live(("public",), "item", admin) == [], "not without the source's scope"


# ---- Linear and HubSpot ----


def linear_server(status: int) -> httpx.MockTransport:
    node = {
        "id": "u1",
        "identifier": "ENG-12",
        "title": "Golden set",
        "description": "Build it",
        "url": "https://linear.example.com/ENG-12",
        "updatedAt": "2026-10-01T10:00:00Z",
        "team": {"key": "ENG"},
        "creator": {"name": "Sam"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "t", "a personal key is sent as it is"
        assert json.loads(request.content)["variables"]["term"] == "golden"
        if status != 200:
            return httpx.Response(status, json={"errors": [{"message": "no"}]})
        return httpx.Response(200, json={"data": {"searchIssues": {"nodes": [node]}}})

    return httpx.MockTransport(handle)


async def test_linear_answers_only_a_holder_of_its_scope(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.add("linear", "internal", "federated", {})
    monkeypatch.setattr(CONNECTORS["linear"], "transport", linear_server(200))
    assert await store.live(("internal",), "golden") == ["ENG-12 Golden set"]
    assert await store.live(("public",), "golden") == [], "another scope reads nothing"

    monkeypatch.setattr(CONNECTORS["linear"], "transport", linear_server(401))
    assert await store.live(("internal",), "golden") == [], "a refused read returns nothing"


def hubspot_server(status: int) -> httpx.MockTransport:
    deal = {
        "id": "9",
        "properties": {
            "dealname": "Renewal",
            "dealstage": "s1",
            "amount": "45000",
            "pipeline": "p",
            "hs_lastmodifieddate": "2026-10-01T10:00:00Z",
        },
    }
    pipelines = [{"id": "p", "label": "Sales", "stages": [{"id": "s1", "label": "Proposal"}]}]
    routes: dict[str, Json] = {
        "POST /crm/v3/objects/deals/search": {"results": [deal]},
        "GET /crm/v3/pipelines/deals": {"results": pipelines},
        "POST /crm/v4/associations/deals/companies/batch/read": {
            "results": [{"from": {"id": "9"}, "to": [{"toObjectId": 5}]}]
        },
        "POST /crm/v3/objects/companies/batch/read": {
            "results": [{"id": "5", "properties": {"name": "Acme"}}]
        },
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer t"
        if status != 200:
            return httpx.Response(status, json={})
        return httpx.Response(200, json=routes[f"{request.method} {request.url.path}"])

    return httpx.MockTransport(handle)


async def test_hubspot_answers_only_a_holder_of_its_scope(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.add("hubspot", "internal", "federated", {"objects": ["deals"], "portal_id": "42"})
    monkeypatch.setattr(CONNECTORS["hubspot"], "transport", hubspot_server(200))
    assert await store.live(("internal",), "renewal") == ["Renewal"]
    assert await store.live(("public",), "renewal") == []
    cur = await federated.search(
        store.conn,
        store.schemas,
        store.env,
        ReadContext((store.tenant,), scopes=("internal",)),
        "renewal",
        seconds=5,
    )
    [hit] = cur[0].hits
    assert "Stage: Proposal" in hit.text and "Company: Acme" in hit.text
    assert hit.url == "https://app.hubspot.com/contacts/42/record/0-3/9"

    monkeypatch.setattr(CONNECTORS["hubspot"], "transport", hubspot_server(403))
    assert await store.live(("internal",), "renewal") == [], "a refused read returns nothing"
