"""The GitHub connectors against a fake GitHub: who reads a repository's records, the live
re-check of a lost grant, and a markdown mirror that is redacted as it is stored."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.data import declared, identity, recheck
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.identity import IdentityKey, Proof
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, Json, Source
from daimon.data.sync.connectors.github_api import API, Api
from daimon.data.sync.credentials import seal
from daimon.data.sync.engine import Env, run_source
from daimon.data.sync.http import Upstream
from daimon.data.sync.rate import RateBudget
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()
ORG = "acme"
STAMP = "2026-01-02T03:04:05Z"


def person(i: int, **extra: object) -> Json:
    return {"id": i, "login": f"u{i}", "permissions": {"pull": True}, **extra}


def sha_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


class FakeGitHub:
    """The GitHub REST and GraphQL calls the connectors make, and a record of the blobs read."""

    def __init__(self) -> None:
        self.repos: dict[int, Json] = {}
        self.readers: dict[int, list[int]] = {}
        self.members = [11, 13]
        self.issues: dict[int, list[Json]] = {}
        self.discussions: dict[int, list[Json]] = {}
        self.files: dict[int, dict[str, str]] = {}
        self.head = "c1"
        self.blobs: list[str] = []

    def repo(self, id_: int, name: str, visibility: str, readers: list[int]) -> None:
        self.repos[id_] = {
            "id": id_,
            "name": name,
            "full_name": f"{ORG}/{name}",
            "visibility": visibility,
            "has_issues": True,
            "default_branch": "main",
        }
        self.readers[id_] = readers
        self.files[id_] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return self.graphql(json.loads(request.content)["variables"]["name"])
        parts = path.strip("/").split("/")
        match parts:
            case ["orgs", _, "repos"]:
                return httpx.Response(200, json=list(self.repos.values()))
            case ["orgs", _, "members"]:
                return httpx.Response(200, json=[person(i) for i in self.members])
            case ["orgs", _, "outside_collaborators"]:
                return httpx.Response(200, json=[])
            case ["orgs", _, "members", login]:
                return httpx.Response(204 if int(login[1:]) in self.members else 404)
            case ["user", i]:
                return httpx.Response(200, json={"id": int(i), "login": f"u{i}"})
            case ["repositories", i]:
                repo = self.repos.get(int(i))
                return httpx.Response(200, json=repo) if repo else httpx.Response(404)
            case ["repositories", i, "collaborators"]:
                return httpx.Response(200, json=[person(u) for u in self.readers[int(i)]])
            case _:
                pass
        id_ = next(
            (i for i, r in self.repos.items() if path.startswith(f"/repos/{r['full_name']}/")), 0
        )
        rest = path.removeprefix(f"/repos/{self.repos[id_]['full_name']}/") if id_ else ""
        if rest == "issues":
            return httpx.Response(200, json=self.issues.get(id_, []))
        if rest.startswith("issues/") and rest.endswith("/comments"):
            return httpx.Response(
                200, json=[{"user": {"login": "u11"}, "body": "rotate them today"}]
            )
        if rest.startswith("commits/"):
            return httpx.Response(
                200, json={"sha": self.head, "commit": {"committer": {"date": STAMP}}}
            )
        if rest.startswith("git/trees/"):
            tree = [
                {"path": p, "sha": sha_of(t), "type": "blob", "size": len(t)}
                for p, t in self.files[id_].items()
            ]
            return httpx.Response(200, json={"tree": tree})
        if rest.startswith("git/blobs/"):
            want = rest.removeprefix("git/blobs/")
            text = next(t for t in self.files[id_].values() if sha_of(t) == want)
            self.blobs.append(want)
            return httpx.Response(
                200,
                json={"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"},
            )
        return httpx.Response(404)

    def graphql(self, name: str) -> httpx.Response:
        id_ = next(i for i, r in self.repos.items() if r["name"] == name)
        nodes = self.discussions.get(id_, [])
        data = {"repository": {"discussions": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}
        return httpx.Response(200, json={"data": data})


def issue(number: int, title: str, *, pull: bool = False) -> Json:
    return {
        "number": number,
        "title": title,
        "body": f"about {title}",
        "state": "open",
        "comments": 1,
        "updated_at": STAMP,
        "html_url": f"https://github.com/{ORG}/x/issues/{number}",
        "user": {"login": "u11"},
        "labels": [],
        **({"pull_request": {}} if pull else {}),
    }


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def add(self, kind: str, **config: object) -> uuid.UUID:
        cur = await self.conn.execute(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, config, credential,"
            " default_scope, publication, redaction_set) values (%s, %s, %s, %s, %s::jsonb, %s,"
            " %s, 'published', %s) returning id".encode(),
            (
                self.tenant,
                kind,
                kind,
                "github",
                Jsonb({k: v for k, v in config.items() if k != "redaction_set"}),
                seal((KEY,), {"token": "t"}),
                "restricted:acl" if kind == "github" else "internal",
                config.get("redaction_set"),
            ),
        )
        row = await cur.fetchone()
        assert row
        return row["id"]

    async def ok(self, src: uuid.UUID) -> None:
        result = await run_source(self.conn, self.env, src)
        assert result.status == "ok", result.error

    async def stored(self, src: uuid.UUID) -> dict[str, str]:
        cur = await self.conn.execute(
            f"select external_id, body from {self.d}.records where source_id = %s".encode(), (src,)
        )
        return {r["external_id"]: r["body"] for r in await cur.fetchall()}

    async def sees(self, user: int) -> set[str]:
        ctx = ReadContext((self.tenant,), readers=(subject("github", ORG, "user", str(user)),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            cur = await c.execute(f"select external_id from {self.d}.records".encode())
            return {r["external_id"] for r in await cur.fetchall()}


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, Env(data_db.schemas, (KEY,)), uuid.uuid4())


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """A private repository (one reader) and a public one, in an organization of two people."""
    fake = FakeGitHub()
    fake.repo(1, "vault", "private", [11])
    fake.repo(2, "site", "public", [12])
    fake.issues = {1: [issue(1, "keys"), issue(2, "fix keys", pull=True)], 2: [issue(1, "logo")]}
    fake.discussions = {
        2: [
            {
                "number": 5,
                "title": "roadmap",
                "body": "ideas",
                "updatedAt": STAMP,
                "comments": {"nodes": []},
            }
        ]
    }
    for i in (1, 2):
        fake.files[i] = {"README.md": f"# repo {i}", "logo.png": "binary"}
    for kind in ("github", "github_markdown"):
        monkeypatch.setattr(CONNECTORS[kind], "transport", httpx.MockTransport(fake))
    return fake


ALL = {"content": ["issues", "pulls", "discussions", "files"], "file_paths": ["README.md"]}


async def test_a_record_is_read_by_the_people_github_lets_read_its_repository(
    store: Store, github: FakeGitHub
) -> None:
    src = await store.add("github", org=ORG, **ALL)
    await store.ok(src)

    stored = await store.stored(src)
    assert set(stored) == {
        "issue:1#1",
        "pull_request:1#2",
        "file:1:README.md",
        "issue:2#1",
        "discussion:2#5",
        "file:2:README.md",
    }, "issues, pull requests, discussions and the listed files, not the picture"
    assert "u11: rotate them today" in stored["issue:1#1"], "the conversation is part of it"
    private = {"issue:1#1", "pull_request:1#2", "file:1:README.md"}
    assert await store.sees(11) == set(stored), "a collaborator, and a member of the public one"
    assert await store.sees(12) == set(stored) - private, "a collaborator of the public repo only"
    assert await store.sees(13) == set(stored) - private, "a member reads the public repo only"
    assert await store.sees(14) == set(), "someone with no access reads nothing"

    github.readers[1] = []
    github.members = [13]
    await store.ok(src)
    assert await store.sees(11) == set(), "a person removed loses it on the next run"
    assert set(await store.stored(src)) == set(stored), "an incremental run removes nothing"


async def test_paging_never_follows_a_next_link_to_another_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com", "the token went to another host"
        return httpx.Response(
            200, json=[{"id": 1}], headers={"link": '<https://other.example.org/x>; rel="next"'}
        )

    source = Source(
        uuid.uuid4(), uuid.uuid4(), "github", "g", "github", {}, {}, "internal", {}, None,
        RateBudget(0),
    )  # fmt: skip
    async with httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Upstream):
            [u async for u in Api(client, source).pages("/orgs/acme/members")]


async def test_a_lost_grant_is_dropped_at_read_before_any_sync(
    store: Store, github: FakeGitHub
) -> None:
    src = await store.add("github", org=ORG, content=["issues"])
    await store.ok(src)
    account = uuid.uuid4()
    await identity.link(
        store.conn,
        store.schemas,
        tenant_id=store.tenant,
        account_id=account,
        proof=Proof(IdentityKey("github", ORG, "11"), "oauth"),
    )
    private, public = (src, "issue:1#1"), (src, "issue:2#1")

    async def shown() -> set[recheck.Ref]:
        return await recheck.allowed(
            store.conn, store.schemas, store.env, (f"account:{account}",), [private, public]
        )

    assert await shown() == {private, public}
    github.readers[1] = []
    assert await shown() == {public}, "GitHub no longer lists them on the private repository"
    github.members = []
    assert await shown() == set(), "nor in the organization that reads the public one"


async def test_a_markdown_mirror_is_redacted_and_follows_the_repository(
    store: Store, github: FakeGitHub
) -> None:
    github.repo(3, "notes", "private", [])
    github.files[3] = {
        "docs/plan.md": "# Plan\nAcme asked for it.",
        "docs/old.md": "gone soon",
        "other/x.md": "outside the folder",
    }
    decl = declared.Declaration.model_validate(
        {
            "revision": 1,
            "tenants": [
                {
                    "workspace": store.tenant,
                    "redaction_sets": [{"id": "house", "rules": {"aliases": {"Acme": "client A"}}}],
                }
            ],
        }
    )
    await declared.apply(store.conn, store.schemas, store.env, decl)
    src = await store.add(
        "github_markdown", repo=f"{ORG}/notes", folders=["docs"], redaction_set="house"
    )
    await store.ok(src)

    stored = await store.stored(src)
    assert set(stored) == {"file:docs/plan.md", "file:docs/old.md"}, "only the listed folder"
    assert "client A" in stored["file:docs/plan.md"], "the source's rule set is applied"
    assert "Acme" not in stored["file:docs/plan.md"]

    read = len(github.blobs)
    await store.ok(src)
    assert len(github.blobs) == read, "an unchanged head reads nothing"

    del github.files[3]["docs/old.md"]
    github.head = "c2"
    await store.ok(src)
    assert set(await store.stored(src)) == {"file:docs/plan.md"}, "a deleted file goes"
    assert len(github.blobs) == read, "and an unchanged file is not read again"
