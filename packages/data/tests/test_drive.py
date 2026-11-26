"""The Google Drive and Calendar connectors against a fake Google: who reads a file, what is
never read, the change feed and the full listing, exports, groups, delegation without a key, the
live re-check of a file, and calendar events shown only to the people on them."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from daimon.data import federated, identity, my_drive, personal, recheck
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.identity import IdentityKey, Proof
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, Json, Source
from daimon.data.sync.connectors.gdrive import GDriveConnector
from daimon.data.sync.credentials import seal
from daimon.data.sync.engine import Env, build_source, ready, run_source
from daimon.data.sync.rate import RateBudget
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()
DOMAIN, ADMIN = "example.com", "admin@example.com"
SERVICE = "reader@project.iam.gserviceaccount.com"
DOC, SHEET, FOLDER, PDF = (
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.folder",
    "application/pdf",
)
SECRET = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_FILE = json.dumps(
    {
        "client_email": SERVICE,
        "private_key": SECRET.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }
)


def user(id_: str, **extra: object) -> Json:
    return {
        "id": id_,
        "primaryEmail": f"{id_}@{DOMAIN}",
        "name": {"fullName": id_.title()},
        **extra,
    }


def grant(kind: str, email: str = "", **extra: object) -> Json:
    return {"type": kind, "role": "reader", "emailAddress": email, **extra}


def to(id_: str) -> Json:
    return grant("user", f"{id_}@{DOMAIN}")


class FakeGoogle:
    """The Google hosts the connector calls, and a record of what it read."""

    def __init__(self) -> None:
        self.users = {
            u["id"]: u for u in (user("admin"), user("alice"), user("bob"), user("carol"))
        }
        self.users["gone"] = user("gone", suspended=True)
        self.groups: dict[str, list[str]] = {}
        self.drives = {"d1": "Team"}
        self.files: dict[str, Json] = {}
        self.feed: list[tuple[str, str, bool]] = []
        """(drive id or owner, file id, removed): the changes each scope's feed reports."""
        self.expired = False
        self.read: list[str] = []
        """Files whose sharing or content was asked for."""
        self.fetched: list[str] = []
        """Files whose content was asked for."""
        self.lists = 0
        self.signed = 0
        self.strict = False
        """Drive opens a file only for the people its sharing names (and the admin)."""
        self.events: list[Json] = []

    def add(
        self,
        id_: str,
        perms: list[Json],
        *,
        name: str = "",
        mime: str = "text/plain",
        **meta: object,
    ) -> None:
        self.files[id_] = {
            "id": id_,
            "name": name or f"{id_}.txt",
            "mimeType": mime,
            "modifiedTime": "2026-01-02T03:04:05Z",
            "parents": [meta.pop("parent", "d1")],
            "driveId": "d1",
            "perms": perms,
            "body": b"",
            **meta,
        }

    def my(self, id_: str, owner: str, perms: list[Json], **meta: Any) -> None:
        self.add(id_, perms, parent="root", owners=[{"emailAddress": owner}], **meta)
        self.files[id_] |= {"driveId": None, "owner": owner}

    def touch(self, id_: str, removed: bool = False) -> None:
        f = self.files[id_]
        self.feed.append((f["driveId"] or f.get("owner", ""), id_, removed))

    def meta(self, f: Json, who: str) -> Json:
        out = {k: v for k, v in f.items() if k not in ("perms", "body", "owner", "driveId")}
        if f["driveId"]:
            out["driveId"] = f["driveId"]
        return out | {"ownedByMe": f.get("owner") == who}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, q = request.url, {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        path = unquote(url.path)
        if url.host == "oauth2.googleapis.com":
            head, claims, sig = parse_qs(request.content.decode())["assertion"][0].split(".")
            pad = "=" * (-len(claims) % 4)
            body = json.loads(base64.urlsafe_b64decode(claims + pad))
            SECRET.public_key().verify(
                base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)),
                f"{head}.{claims}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return httpx.Response(200, json={"access_token": f"tok-{body['sub']}"})
        if url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "runtime"})
        if url.host == "iamcredentials.googleapis.com":
            self.signed += 1
            assert request.headers["authorization"] == "Bearer runtime"
            claims = base64.urlsafe_b64encode(json.loads(request.content)["payload"].encode())
            head = base64.urlsafe_b64encode(b"{}")
            sig = SECRET.sign(head + b"." + claims, padding.PKCS1v15(), hashes.SHA256())
            token = f"{head.decode()}.{claims.decode()}.{base64.urlsafe_b64encode(sig).decode()}"
            return httpx.Response(200, json={"signedJwt": token})
        who = request.headers["authorization"].removeprefix("Bearer tok-")
        if path.startswith("/admin/directory/v1"):
            return self.directory(path, who)
        if path.startswith("/calendar/v3/calendars/primary"):
            return self.calendar(path, who)
        return self.drive(path, q, who)

    def directory(self, path: str, who: str) -> httpx.Response:
        assert who == ADMIN, "the directory is read as the admin"
        if "/users/" in path:
            found = self.users.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path.endswith("/users"):
            return httpx.Response(200, json={"users": list(self.users.values())})
        if path.endswith("/groups"):
            return httpx.Response(200, json={"groups": [{"email": e} for e in self.groups]})
        email = path.split("/groups/")[1].removesuffix("/members")
        return httpx.Response(
            200, json={"members": [{"email": m, "type": "USER"} for m in self.groups[email]]}
        )

    def calendar(self, path: str, who: str) -> httpx.Response:
        mine = [e for e in self.events if who in [a["email"] for a in e.get("attendees", [])]]
        return httpx.Response(200, json={"items": mine} if path.endswith("/events") else {})

    def drive(self, path: str, q: dict[str, str], who: str) -> httpx.Response:
        if path == "/drive/v3/drives":
            assert who == ADMIN and q["useDomainAdminAccess"] == "true"
            return httpx.Response(
                200, json={"drives": [{"id": i, "name": n} for i, n in self.drives.items()]}
            )
        if path == "/drive/v3/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.feed))})
        if path == "/drive/v3/changes":
            if self.expired:
                return httpx.Response(400, json={"error": {"errors": [{"reason": "badRequest"}]}})
            scope = q.get("driveId") or who
            found = [
                {
                    "fileId": i,
                    "removed": gone,
                    "file": None if gone else self.meta(self.files[i], who),
                }
                for s, i, gone in self.feed[int(q["pageToken"]) :]
                if s == scope
            ]
            return httpx.Response(
                200, json={"changes": found, "newStartPageToken": str(len(self.feed))}
            )
        if path == "/drive/v3/files":
            self.lists += 1
            words = q.get("q", "").partition("fullText contains '")[2].removesuffix("'")
            mine = [
                f
                for f in self.files.values()
                if (
                    f["driveId"] == q["driveId"]
                    if q["corpora"] == "drive"
                    else f.get("owner") == who
                )
                and words.lower() in (f["name"] + f["body"].decode()).lower()
            ]
            return httpx.Response(
                200, json={"files": [self.meta(f, who) for f in mine if not f.get("trashed")]}
            )
        fid, _, rest = path.removeprefix("/drive/v3/files/").partition("/")
        f = self.files.get(fid)
        if f is None:
            return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
        if rest == "" and q.get("alt") != "media":
            named = [p.get("emailAddress") for p in f["perms"]]
            if self.strict and who != ADMIN and who not in named:
                return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
            return httpx.Response(200, json=self.meta(f, who))
        self.read.append(fid)
        if rest != "permissions":
            self.fetched.append(fid)
        if rest == "permissions":
            assert not f["driveId"] or q["useDomainAdminAccess"] == "true"
            return httpx.Response(200, json={"permissions": f["perms"]})
        return httpx.Response(200, content=f["body"])


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def run(self, src: uuid.UUID) -> None:
        result = await run_source(self.conn, self.env, src)
        assert result.status == "ok", result.error

    async def bodies(self, src: uuid.UUID) -> dict[str, str]:
        cur = await self.conn.execute(
            f"select external_id, body from {self.d}.records where source_id = %s".encode(), (src,)
        )
        return {r["external_id"]: r["body"] for r in await cur.fetchall()}

    async def sees(self, workspace: str, user: str) -> set[str]:
        ctx = ReadContext((self.tenant,), readers=(subject("google", workspace, "user", user),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            cur = await c.execute(f"select external_id from {self.d}.records".encode())
            return {r["external_id"] for r in await cur.fetchall()}

    async def accounts(self) -> dict[str, tuple[str | None, bool]]:
        cur = await self.conn.execute(
            f"select subject, email, active from {self.d}.identities"
            " where tenant_id = %s and provider = 'google'".encode(),
            (self.tenant,),
        )
        return {r["subject"]: (r["email"], r["active"]) for r in await cur.fetchall()}


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, Env(data_db.schemas, (KEY,)), uuid.uuid4())


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    """A domain with four active people and a suspended one, and one shared drive."""
    fake = FakeGoogle()
    for kind in ("gdrive", "gcal"):
        monkeypatch.setattr(CONNECTORS[kind], "transport", httpx.MockTransport(fake))
    return fake


async def add_source(store: Store, **config: object) -> uuid.UUID:
    cur = await store.conn.execute(
        f"insert into {store.d}.sources (tenant_id, kind, name, provider, config, credential,"
        " default_scope, publication) values (%s, 'gdrive', 'drive', 'google', %s::jsonb, %s,"
        " 'internal', 'published') returning id".encode(),
        (
            store.tenant,
            Jsonb({"domain": DOMAIN, "admin": ADMIN, **config}),
            seal((KEY,), {"service_account_key": KEY_FILE}),
        ),
    )
    row = await cur.fetchone()
    assert row
    return row["id"]


async def readers(store: Store, people: list[str]) -> dict[str, set[str]]:
    return {p: await store.sees(DOMAIN, p) for p in people}


PEOPLE = ["alice", "bob", "carol", "gone"]


async def test_a_file_is_read_by_the_people_its_sharing_names(
    store: Store, google: FakeGoogle
) -> None:
    google.groups = {f"eng@{DOMAIN}": [f"bob@{DOMAIN}", f"gone@{DOMAIN}"]}
    google.add("direct", [to("alice"), to("gone"), grant("user", "outsider@other.org")])
    google.add("by_group", [grant("group", f"eng@{DOMAIN}")])
    google.add("domain", [grant("domain", domain=DOMAIN, allowFileDiscovery=True)])
    google.add("link_only", [grant("anyone"), grant("domain", domain=DOMAIN)])
    google.add("expiring", [{**to("alice"), "expirationTime": "2030-01-01T00:00:00Z"}])
    google.add("alias", [grant("user", f"alice.b@{DOMAIN}")])
    google.my("mine", f"alice@{DOMAIN}", [to("alice"), to("bob")])
    src = await add_source(store)

    await store.run(src)

    assert set(await store.bodies(src)) == {
        "file:direct",
        "file:by_group",
        "file:domain",
    }, "a file nobody inside may read (link-only, expiring, alias, outsiders), or of a My Drive"
    assert await readers(store, PEOPLE) == {
        "alice": {"file:direct", "file:domain"},
        "bob": {"file:by_group", "file:domain"},
        "carol": {"file:domain"},
        "gone": set(),
    }, "a suspended person reads nothing, even when named or in a group"
    assert await store.accounts() == {
        "admin": (f"admin@{DOMAIN}", True),
        "alice": (f"alice@{DOMAIN}", True),
        "bob": (f"bob@{DOMAIN}", True),
        "carol": (f"carol@{DOMAIN}", True),
        "gone": (f"gone@{DOMAIN}", False),
    }


async def test_a_group_grants_only_through_the_sources_own_membership(
    store: Store, google: FakeGoogle
) -> None:
    google.groups = {f"eng@{DOMAIN}": [f"bob@{DOMAIN}"]}
    google.add("f", [grant("group", f"eng@{DOMAIN}")])
    src = await add_source(store)
    await store.run(src)
    assert (await readers(store, PEOPLE))["bob"] == {"file:f"}

    google.groups[f"eng@{DOMAIN}"] = [f"carol@{DOMAIN}"]
    await store.run(src)
    now = await readers(store, PEOPLE)
    assert (now["bob"], now["carol"]) == (set(), {"file:f"}), "the file did not change"


async def test_folders_left_out_are_never_read(store: Store, google: FakeGoogle) -> None:
    google.add("keep", [to("alice")], name="kept", parent="in")
    google.add("secret", [to("alice")], name="secret", parent="out")
    google.add("outside", [to("alice")], name="outside", parent="elsewhere")
    google.add("in", [to("alice")], mime=FOLDER, parent="d1")
    google.add("out", [to("alice")], mime=FOLDER, parent="in")
    src = await add_source(store, include_folders=["in"], exclude_folders=["out"])

    await store.run(src)

    assert set(await store.bodies(src)) == {"file:keep"}
    assert set(google.read) == {"keep"}, "no sharing or content of an excluded file was asked for"


async def test_changes_follow_the_feed_and_a_lost_cursor_lists_everything_again(
    store: Store, google: FakeGoogle
) -> None:
    google.add("a", [to("alice")])
    google.add("b", [to("alice")])
    src = await add_source(store)
    await store.run(src)
    assert google.lists == 1

    google.add("c", [to("alice")])
    google.touch("c")
    google.touch("b", removed=True)
    del google.files["b"]
    await store.run(src)
    assert set(await store.bodies(src)) == {"file:a", "file:c"}, "an added and a removed file"
    assert google.lists == 1, "an unchanged scope is not listed again"

    del google.files["a"]
    google.expired = True
    await store.run(src)
    assert set(await store.bodies(src)) == {"file:c"}, "a deletion the feed lost is pruned"
    assert google.lists == 2


async def test_google_formats_are_exported_and_files_are_read_to_their_cap(
    store: Store, google: FakeGoogle
) -> None:
    google.add("doc", [to("alice")], mime=DOC, body=b"quarterly plan")
    google.add("notes", [to("alice")], name="notes.txt", body=b"plain notes")
    google.add("big", [to("alice")], name="big.txt", size="3000000", body=b"x" * 3_000_000)
    google.add("scan", [to("alice")], name="scan.pdf", mime=PDF, body=b"%PDF")
    src = await add_source(store, max_mb=1)

    await store.run(src)

    bodies = await store.bodies(src)
    assert bodies["file:doc"] == "quarterly plan"
    cur = await store.conn.execute(
        f"select external_id, extracted from {store.d}.attachments where source_id = %s".encode(),
        (src,),
    )
    assert {r["external_id"]: r["extracted"] for r in await cur.fetchall()} == {
        "file:notes": True
    }, "text is taken from a text file; a PDF and a file over the cap keep their title"
    assert set(bodies) == {"file:doc", "file:notes", "file:big", "file:scan"}
    assert set(google.fetched) == {"doc", "notes"}, "no bytes of a PDF or an oversized file"


async def test_delegation_without_a_key_signs_through_iam_and_a_person_can_be_checked(
    google: FakeGoogle,
) -> None:
    google.add("f", [to("alice")])
    source = Source(
        uuid.uuid4(),
        uuid.uuid4(),
        "gdrive",
        "drive",
        "google",
        {"domain": DOMAIN, "admin": ADMIN, "service_account": SERVICE},
        {},
        "internal",
        {},
        None,
        RateBudget(0),
        keyless_accounts=(SERVICE,),
    )
    connector = CONNECTORS["gdrive"]
    assert isinstance(connector, GDriveConnector)

    refused, why = await connector.test(replace(source, keyless_accounts=()))
    assert not refused and "does not allow keyless signing" in why, why
    assert google.signed == 0, "an account the deployment does not allow is never signed as"

    ok, message = await connector.test(source)
    assert (ok, message) == (True, "4 people, 1 shared drives to read"), message
    assert google.signed >= 1, "the assertion was signed by the IAM Credentials API"
    assert await connector.can_open(source, "file:f", ["alice"])
    assert not await connector.can_open(source, "file:nothing", ["alice"])
    assert await connector.can_open(source, "file:f", ["gone"]) is None, "a suspended person"


async def test_keyless_accounts_are_allowed_by_daimon_workspace_never_by_the_domain_typed(
    store: Store, google: FakeGoogle
) -> None:
    src = await add_source(store)
    await store.conn.execute(
        b"insert into tenants (id, platform, external_id) values (%s, 'discord', %s)",
        (store.tenant, f"guild-{store.tenant.hex[:8]}"),
    )
    cur = await store.conn.execute(
        f"select s.*, null::jsonb as state, null::timestamptz as last_sync_at"
        f" from {store.d}.sources s where s.id = %s".encode(),
        (src,),
    )
    row = await cur.fetchone()
    assert row

    async def allowed(*entries: str) -> tuple[str, ...]:
        env = replace(store.env, keyless_accounts=entries)
        return (await ready(store.conn, env, build_source(row, env, None))).keyless_accounts

    other = uuid.uuid4()
    assert await allowed(f"{other}={SERVICE}", f"discord:guild-x={SERVICE}") == ()
    assert await allowed(f"{store.tenant}={SERVICE}") == (SERVICE,)
    assert await allowed(f"discord:guild-{store.tenant.hex[:8]}={SERVICE}") == (SERVICE,)


async def link(store: Store, person: str) -> str:
    """A reader token for the Daimon account that signed in as this directory user."""
    account = uuid.uuid4()
    await identity.link(
        store.conn,
        store.schemas,
        tenant_id=store.tenant,
        account_id=account,
        proof=Proof(IdentityKey("google", DOMAIN, person), "oauth"),
    )
    return f"account:{account}"


async def test_a_file_the_source_no_longer_opens_to_a_person_is_dropped_at_read(
    store: Store, google: FakeGoogle
) -> None:
    google.add("f", [to("alice"), to("bob")])
    src = await add_source(store)
    await store.run(src)
    alice, bob = await link(store, "alice"), await link(store, "bob")
    google.strict = True

    async def shown(*readers: str) -> set[recheck.Ref]:
        return await recheck.allowed(
            store.conn, store.schemas, store.env, readers, [(src, "file:f")]
        )

    assert await shown(alice, bob) == {(src, "file:f")}, "Drive opens it to both"
    google.files["f"]["perms"] = [to("alice")]
    assert await shown(alice) == {(src, "file:f")}
    assert await shown(alice, bob) == set(), "a grant Drive took back ends the read"


async def test_an_event_shows_only_to_the_people_on_it(store: Store, google: FakeGoogle) -> None:
    google.events = [
        {
            "id": "e1",
            "summary": "Planning standup",
            "start": {"dateTime": "2026-02-03T10:00:00Z"},
            "attendees": [{"email": f"alice@{DOMAIN}"}, {"email": f"bob@{DOMAIN}"}],
        },
        {
            "id": "e2",
            "summary": "Planning review",
            "start": {"dateTime": "2026-02-04T10:00:00Z"},
            "attendees": [{"email": f"alice@{DOMAIN}"}],
        },
    ]
    await store.conn.execute(
        f"insert into {store.d}.sources (tenant_id, kind, name, provider, config, credential,"
        " default_scope, mode, publication) values (%s, 'gcal', 'calendar', 'google', %s::jsonb,"
        " %s, 'internal', 'federated', 'published')".encode(),
        (
            store.tenant,
            Jsonb({"domain": DOMAIN, "admin": ADMIN}),
            seal((KEY,), {"service_account_key": KEY_FILE}),
        ),
    )
    alice = await link(store, "alice")
    bob = await link(store, "bob")
    carol = await link(store, "carol")

    async def titles(*readers: str, off: tuple[personal.Part, ...] = ()) -> set[str]:
        ctx = ReadContext((store.tenant,), scopes=("internal",), readers=readers)
        found = await federated.search(
            store.conn, store.schemas, store.env, ctx, "planning", seconds=5, off_parts=off
        )
        return {h.title for r in found for h in r.hits}

    assert await titles(alice) == {"Planning standup", "Planning review"}
    assert await titles(alice, off=("calendar",)) == set(), "the workspace turned calendars off"
    assert await titles(carol) == set(), "not on any event"
    assert await titles(alice, bob) == {"Planning standup"}, "only what every reader may see"
    assert await titles() == set(), "no reader, no answer"
    await personal.set_part(
        store.conn, store.schemas, store.tenant, uuid.UUID(bob.removeprefix("account:")),
        "calendar", False,
    )  # fmt: skip
    assert await titles(alice, bob) == set(), "a reader who turned their calendar off"
    assert await titles(alice) == {"Planning standup", "Planning review"}, "only theirs is off"


async def test_a_persons_own_drive_is_searched_as_them_and_never_stored(
    store: Store, google: FakeGoogle
) -> None:
    google.my("plan", f"alice@{DOMAIN}", [to("alice")], mime=DOC, body=b"budget plan")
    google.my("other", f"alice@{DOMAIN}", [to("alice")], name="lunch.txt")
    google.my("bobs", f"bob@{DOMAIN}", [to("bob")], mime=DOC, body=b"budget too")
    src = await add_source(store)
    await store.run(src)
    alice = uuid.UUID((await link(store, "alice")).removeprefix("account:"))

    hits = await my_drive.search(
        store.conn, store.schemas, store.env, store.tenant, alice, "budget"
    )
    assert [(h.ref, h.text) for _, h in hits] == [("file:plan", "budget plan")], "hers, as her"
    assert {s for s, _ in hits} == {src}, "with the source it was read through"
    assert await store.bodies(src) == {}, "nothing of a My Drive is stored"
    await personal.set_part(store.conn, store.schemas, store.tenant, alice, "drive", False)
    mine = await my_drive.search(
        store.conn, store.schemas, store.env, store.tenant, alice, "budget"
    )
    assert mine == [], "she turned her own Drive off"
    shown = await personal.parts(store.conn, store.schemas, store.tenant, alice)
    assert shown == {"drive": False, "calendar": True}, "each part is on until she turns it off"
    await personal.set_part(store.conn, store.schemas, store.tenant, alice, "drive", True)
    shown = await personal.parts(store.conn, store.schemas, store.tenant, alice, ["calendar"])
    assert shown == {"calendar": True}, "a part the workspace turned off is not offered"
