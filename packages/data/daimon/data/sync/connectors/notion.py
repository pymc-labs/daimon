"""Notion: the pages and databases an integration can see, readable by the people who have
signed in to Notion through Daimon.

The integration token only discovers: it lists what Notion shares with the integration (search),
reads each changed page's text, and grants nobody anything. Every object has one access list, the
group of people with a live Notion sign-in (the people signed in at this source, each proving
themselves with their own token: Notion must accept it on every run, and a token that stops
working ends that person's access with their unit, within `UNIT_MINUTES`). A person who has not
signed in reads no Notion. `exclude` lists page or database ids whose whole tree is stored but
read by nobody. An object that disappears from a complete listing is removed; a page whose
text Notion refuses to give keeps what it had.

Create an internal integration with read content access and share the pages to read with it.
Credential `{"token": "secret_..."}`; configuration (`sources.config`):

- `workspace`: a name for the Notion workspace (its identities are keyed by it).
- `exclude`: page or database ids to store but never show.

Changes are found by their last edit time. See `docs/data-sources.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

import httpx
import structlog
from daimon.data.access import subject
from daimon.data.identity import SourceAccount
from daimon.data.sync.connector import (
    Acl,
    AclRef,
    Checkpoint,
    Connector,
    Descriptor,
    Field,
    ItemRecord,
    Json,
    PermissionUnit,
    Record,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import names, obj, rows
from daimon.data.sync.http import GuardedTransport, Upstream

log = structlog.get_logger()

PROVIDER: Final = "notion"
API: Final = "https://api.notion.com/v1"
MCP: Final = "https://mcp.notion.com/mcp"
VERSION: Final = "2022-06-28"
PACE: Final = 0.4
"""Seconds between calls: the integration's limit is about three a second."""
UNIT_MINUTES: Final = 8 * 60
"""How long a good sign-in keeps its person readable (an access token's life)."""
TEXT_MAX: Final = 100_000
DEPTH: Final = 12
"""The deepest tree of parents followed."""
BLOCK_DEPTH: Final = 4
OVERLAP: Final = timedelta(minutes=2)
"""Notion's edit times have minute precision: an object edited this recently is read again."""
CHECK_EVERY: Final = 25
CHECK_SECONDS: Final = 20.0


class NotFound(Exception):
    """Deleted, or not shared with the integration (Notion does not say which)."""


def canon(i: str) -> str:
    s = i.replace("-", "").lower()
    if len(s) == 32 and s.isalnum():
        return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"
    return i


def connected_group(ws: str) -> str:
    return subject(PROVIDER, ws, "group", "connected")


def excluded_group(ws: str) -> str:
    """A group nobody is published into: a conjunction with its leaf reads as nobody."""
    return subject(PROVIDER, ws, "group", "excluded")


def object_acl(ws: str, excluded: bool) -> Acl:
    connected = Acl((AclRef(connected_group(ws), group=True),))
    if not excluded:
        return connected
    return Acl(all_of=(connected, Acl((AclRef(excluded_group(ws), group=True),))))


@dataclass(frozen=True)
class Config:
    workspace: str
    exclude: frozenset[str]

    @classmethod
    def of(cls, config: Json) -> Config:
        ws = str(config.get("workspace") or "").strip()
        if not ws:
            raise ValueError("config.workspace must name the Notion workspace")
        return cls(ws, frozenset(canon(x) for x in names(config, "exclude")))

    @property
    def signature(self) -> str:
        return hashlib.sha256("|".join(sorted(self.exclude)).encode()).hexdigest()[:16]


def token_of(source: Source) -> str:
    token = source.credentials.get("token", "")
    if not token:
        raise ValueError("the credential needs the integration's token")
    return token


# ---- the integration's API -----------------------------------------------------------------


class Api:
    """Calls inside the source's budget and the integration's pace."""

    def __init__(self, client: httpx.AsyncClient, source: Source, pace: float) -> None:
        self.client, self.source, self.pace, self._next = client, source, pace, 0.0

    async def request(self, method: str, path: str, body: Json | None = None) -> Json:
        for _ in range(4):
            await self.source.budget.take()
            if (wait := self._next - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            r = await self.client.request(method, path, json=body)
            self._next = time.monotonic() + self.pace
            if r.status_code == 429:
                await asyncio.sleep(min(float(r.headers.get("retry-after") or 1), 60))
                continue
            if r.status_code == 200:
                return obj(r.json())
            if r.status_code == 404 or (r.status_code == 403 and _code(r) == "restricted_resource"):
                raise NotFound(path)
            raise Upstream(f"Notion answered HTTP {r.status_code} for {path}")
        raise Upstream("Notion rate limited the integration")

    async def paged(self, method: str, path: str, body: Json | None = None) -> AsyncIterator[Json]:
        cursor: str | None = None
        while True:
            q: Json = {"start_cursor": cursor} if cursor else {}
            page = await self.request(
                method,
                f"{path}?start_cursor={cursor}" if method == "GET" and cursor else path,
                {**(body or {}), **q} if method == "POST" else None,
            )
            for item in rows(page.get("results")):
                yield item
            cursor = str(page["next_cursor"]) if page.get("has_more") else None
            if not cursor:
                return


def _code(r: httpx.Response) -> str | None:
    try:
        code = obj(r.json()).get("code")
    except ValueError:
        return None
    return code if isinstance(code, str) else None


# ---- what an object looks like -------------------------------------------------------------


def plain(rich: object) -> str:
    return "".join(str(x.get("plain_text", "")) for x in rows(rich))


def title_of(o: Json) -> str:
    if o.get("object") == "database":
        return plain(o.get("title"))
    for p in obj(o.get("properties")).values():
        if obj(p).get("type") == "title":
            return plain(obj(p).get("title"))
    return ""


def when(s: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=UTC)


def property_text(props: object) -> list[str]:
    """`Name: value` for the plain properties of a row (no people, relations or formulas)."""
    out: list[str] = []
    for name, raw in obj(props).items():
        p = obj(raw)
        kind = str(p.get("type"))
        v: object = None
        match kind:
            case "rich_text":
                v = plain(p.get("rich_text"))
            case "select" | "status":
                v = obj(p.get(kind)).get("name")
            case "multi_select":
                v = ", ".join(str(x.get("name", "")) for x in rows(p.get("multi_select")))
            case "number" | "checkbox" | "url" | "email" | "phone_number":
                v = p.get(kind)
            case "date":
                v = obj(p.get("date")).get("start")
            case _:
                pass
        if v not in (None, "", False):
            out.append(f"{name}: {v}")
    return out


def render(block: Json) -> str:
    kind = str(block.get("type", ""))
    body = obj(block.get(kind))
    text = plain(body.get("rich_text"))
    if not text:
        return ""
    if kind.startswith("heading_"):
        return f"{'#' * int(kind[-1])} {text}"
    return {
        "bulleted_list_item": f"- {text}",
        "numbered_list_item": f"1. {text}",
        "quote": f"> {text}",
        "to_do": f"- [ ] {text}",
        "code": f"```\n{text}\n```",
    }.get(kind, text)


async def lines(api: Api, block_id: str, depth: int = 0) -> list[str]:
    """The text of a page's blocks (child pages and databases are objects of their own)."""
    out: list[str] = []
    async for b in api.paged("GET", f"/blocks/{block_id}/children?page_size=100"):
        if b.get("type") in ("child_page", "child_database"):
            continue
        if line := render(b):
            out.append(line)
        if b.get("has_children") and depth < BLOCK_DEPTH:
            out += ["  " + x for x in await lines(api, str(b["id"]), depth + 1)]
    return out


@dataclass
class Tree:
    """The listed objects and where each sits."""

    objs: dict[str, Json]
    api: Api
    memo: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    blocks: dict[str, Json] = field(default_factory=dict[str, Json])

    async def ancestors(self, i: str, depth: int = 0) -> tuple[str, ...]:
        """The parents of `i`, the topmost first, as far as the integration can see them."""
        if i in self.memo:
            return self.memo[i]
        parent = obj(self.objs[i].get("parent"))
        for _ in range(DEPTH):
            kind = str(parent.get("type") or "")
            if kind == "block_id":
                bid = canon(str(parent.get(kind)))
                if bid not in self.blocks:
                    try:
                        self.blocks[bid] = await self.api.request("GET", f"/blocks/{bid}")
                    except NotFound:
                        self.blocks[bid] = {}
                parent = obj(self.blocks[bid].get("parent"))
                continue
            break
        kind = str(parent.get("type") or "")
        pid = canon(str(parent.get(kind) or "")) if kind not in ("", "workspace") else ""
        up: tuple[str, ...] = ()
        if pid in self.objs and depth < DEPTH:
            up = (*await self.ancestors(pid, depth + 1), pid)
        self.memo[i] = up
        return up


def is_trashed(o: Json) -> bool:
    return bool(o.get("in_trash") or o.get("archived"))


async def accepted(transport: httpx.AsyncBaseTransport, token: str) -> bool:
    """Whether Notion accepts this person's token now: its MCP server opens a session for it."""
    try:
        async with httpx.AsyncClient(timeout=8, transport=transport) as h:
            r = await h.post(
                MCP,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "Daimon", "version": "1"},
                    },
                },
            )
    except httpx.HTTPError:
        return False
    return r.status_code == 200 and bool(r.headers.get("mcp-session-id"))


# ---- the connector ---------------------------------------------------------------------------


class NotionConnector(Connector):
    kind: ClassVar[str] = "notion"
    name: ClassVar[str] = "Notion"
    category: ClassVar[str] = "docs"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    person_sign_in: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Integration token", "secret"),
        Field("workspace", "Workspace name"),
        Field("exclude", "Never show", "list", required=False, placeholder="page or database IDs"),
    )

    def __init__(
        self, transport: httpx.AsyncBaseTransport | None = None, pace: float = PACE
    ) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport, self.pace = transport, pace

    def _guard(self, source: Source) -> httpx.AsyncBaseTransport:
        return self.transport or GuardedTransport(source.private_hosts)

    def _client(self, source: Source) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {token_of(source)}", "Notion-Version": VERSION},
            timeout=30,
            transport=self._guard(source),
        )

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            Config.of(source.config)
            async with self._client(source) as client:
                await Api(client, source, 0).request("GET", "/users/me")
        except (ValueError, Upstream, NotFound, httpx.HTTPError) as e:
            return False, str(e) or "the integration token was refused"
        return True, "ok"

    # ---- who reads ----

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit per person signed in, each publishing them into the connected group, but
        only after Notion accepts their token now. A sign-in that stopped proving itself
        publishes an empty unit with no lifetime: their access ends at once."""
        cfg = Config.of(source.config)
        group = connected_group(cfg.workspace)
        for p in source.people:
            ref = f"person:{p.account_id}"
            if p.token is None:
                yield PermissionUnit("connection", ref, 0, {group: ()})
            elif await accepted(self._guard(source), p.token):
                yield PermissionUnit(
                    "connection",
                    ref,
                    UNIT_MINUTES,
                    {group: (subject(PROVIDER, cfg.workspace, "user", p.subject),)},
                    time.time_ns() // 1000,
                )

    async def accounts(self, source: Source) -> Roster | None:
        cfg = Config.of(source.config)
        out: list[SourceAccount] = []
        async with self._client(source) as client:
            async for u in Api(client, source, self.pace).paged("GET", "/users?page_size=100"):
                person = obj(u.get("person"))
                if u.get("type") == "person" and person.get("email"):
                    out.append(
                        SourceAccount(
                            str(u["id"]),
                            str(u.get("name") or ""),
                            str(person["email"]),
                            bool(person.get("email_verified")),
                        )
                    )
        return Roster(PROVIDER, cfg.workspace, tuple(out))

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config.of(source.config)
        old = source.state
        same = old.get("ex") == cfg.signature
        done: dict[str, str] = dict(obj(old.get("edited"))) if same else {}
        state: Json = {"v": 1, "ex": cfg.signature, "edited": done}
        async with self._client(source) as client:
            api = Api(client, source, self.pace)
            await api.request("GET", "/users/me")  # a refused token fails the run first
            found: dict[str, Json] = {}
            async for o in api.paged("POST", "/search", {"page_size": 100}):
                if o.get("id") and not is_trashed(o):
                    found[canon(str(o["id"]))] = o
            tree = Tree(found, api)
            live: set[str] = set()
            n, last = 0, time.monotonic()
            for i in sorted(found):
                o = found[i]
                kind = "database" if o.get("object") == "database" else "page"
                live.add(f"{kind}:{i}")
                edited = when(o.get("last_edited_time")) or datetime.min.replace(tzinfo=UTC)
                if done.get(i) == edited.isoformat():
                    continue
                try:
                    body = await self._text(api, o, i, kind)
                except NotFound:  # no longer shared: it keeps what it had
                    continue
                anc = await tree.ancestors(i)
                rec = self._record(cfg, o, i, kind, body, edited, anc, found)
                yield rec
                if datetime.now(UTC) - edited > OVERLAP:
                    done[i] = edited.isoformat()
                n += 1
                if n % CHECK_EVERY == 0 or time.monotonic() - last >= CHECK_SECONDS:
                    last = time.monotonic()
                    yield Checkpoint(state)
        state["edited"] = {i: t for i, t in done.items() if i in found}
        yield SyncState(state, live)

    @staticmethod
    async def _text(api: Api, o: Json, i: str, kind: str) -> str:
        if kind == "database":
            return ""
        props = property_text(o.get("properties"))
        return "\n\n".join([*props, *await lines(api, i)])[:TEXT_MAX]

    @staticmethod
    def _record(
        cfg: Config,
        o: Json,
        i: str,
        kind: str,
        body: str,
        edited: datetime,
        anc: tuple[str, ...],
        found: dict[str, Json],
    ) -> ItemRecord:
        excluded = i in cfg.exclude or any(a in cfg.exclude for a in anc)
        root = anc[0] if anc else i
        return ItemRecord(
            f"{kind}:{i}",
            kind,
            title_of(o) or "Untitled",
            body,
            edited,
            container=title_of(found[root]) or root if root in found else root,
            container_kind="page",
            url=str(o["url"]) if o.get("url") else None,
            payload={"parent": obj(o.get("parent")), "root": root, "ancestors": list(anc)},
            acl=object_acl(cfg.workspace, excluded),
        )
