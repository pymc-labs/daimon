"""Bluedot: meeting summaries and transcripts, readable by the people Bluedot lets read them,
proved by each person's own sign-in.

Nothing is read with a shared key. Each person signed in at the source (`Source.people`) is
read through Bluedot's MCP server with their own token: their `list_collections` (with the
sub-collections they nest) and `list_meetings`. A person reads a meeting when its collection is
in their list. Each run
publishes every completed list as one permission unit that lives `LEASE_MINUTES` (a group per
collection, holding the person), so a person whose list is not renewed, or whose sign-in stops
working, stops reading. A meeting of a collection is readable by that collection's group; any
other (private, or in no collection) by the people who list it. A meeting listed by nobody who
is signed in is not stored.

A meeting is read once, through a person who lists it, and read again when its summary or
transcript was missing (for `WAIT_DAYS`) or when who may read it changed. A meeting is removed
only by a run in which every sign-in listed completely; a person whose list failed removes
nothing. A meeting Bluedot refuses to give is left out of the run, so a complete run removes it.

Credential: none; people sign in with their own account. Configuration (`sources.config`):

- `workspace`: the Bluedot workspace's id (needed to list its participants as accounts).
- `domain`: an email domain whose addresses count as verified for linking accounts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final, Self, cast

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
    Person,
    Record,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.http import GuardedTransport

log = structlog.get_logger()

PROVIDER: Final = "bluedot"
MCP: Final = "https://app.bluedothq.com/api/v1/mcp"
PROTOCOL: Final = "2025-06-18"
LEASE_MINUTES: Final = 24 * 60
"""How long a person's completed list keeps them reading."""
PAGE: Final = 16
"""`list_meetings` items a page (the server's maximum)."""
MARGIN: Final = 60.0
"""Seconds of a run kept back from listing for the meetings it changed."""
WAIT_DAYS: Final = 3
"""A meeting without a summary or transcript is read again for this long."""
BATCH: Final = 25
EARLIEST: Final = datetime(2015, 1, 1, tzinfo=UTC)
TIME_KEYS: Final = ("createdAt", "meetingTime", "dateCreated", "startTime")


class BluedotError(Exception):
    """The server refused or failed; the message is a short code, never a body."""


class NotFound(BluedotError):
    """The meeting does not exist, or this person cannot open it (Bluedot does not say which)."""


class OutOfTime(BluedotError):
    """The run's deadline is near: the list is unfinished and proves nothing."""


# ---- what Bluedot says -----------------------------------------------------------------------


def clean_email(x: object) -> str | None:
    e = str(x or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+", e) else None


def stamp(x: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def meeting_time(x: object) -> datetime | None:
    """A meeting's time: ISO text, or Unix time as a number or digit string (above 1e11 is
    milliseconds), from 2015 to a day ahead."""
    if isinstance(x, str):
        if (t := stamp(x)) or not re.fullmatch(r"\d+(\.\d+)?", x.strip()):
            return t
        x = float(x)
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    try:
        t = datetime.fromtimestamp(x / 1000 if x > 1e11 else x, UTC) if math.isfinite(x) else None
    except (OverflowError, OSError, ValueError):
        return None
    return t if t and EARLIEST <= t <= datetime.now(UTC) + timedelta(days=1) else None


def text_of(x: object) -> str:
    d = obj(x)
    if isinstance(x, dict):
        x = d.get("text") or d.get("content") or ""
    return re.sub(r"[ \t]+\n", "\n", str(x or "")).strip()


def summary_text(x: object) -> str:
    """The summary: text, or `get_meeting`'s `{entries: [{name, blocks: [{type, text}]}]}`."""
    entries = rows(obj(x).get("entries"))
    if entries:
        return "\n\n".join(t for e in entries for b in rows(e.get("blocks")) if (t := text_of(b)))
    return text_of(x)


def turns(x: object) -> str:
    """The transcript, one speaker turn per line (a speaker's run of segments is one turn)."""
    if isinstance(x, str):
        return x.strip()
    out: list[list[str]] = []
    for t in rows(x):
        if text := text_of(t.get("text")):
            who = str(t.get("speaker") or t.get("speakerTag") or "Speaker")
            if out and out[-1][0] == who:
                out[-1][1] += " " + text
            else:
                out.append([who, text])
    return "\n".join(f"{who}: {text}" for who, text in out)


def uploader_of(detail: Json) -> str | None:
    """Who uploaded it, lowercase: an email when Bluedot gives one, else its id."""
    x = detail.get("uploadedBy")
    d = obj(x)
    x = (d.get("email") or d.get("id")) if isinstance(x, dict) else x
    return clean_email(x) or (str(x).strip().lower() if x else None)


def collection_ids(listed: list[Json]) -> list[str]:
    """The ids of the listed collections and, at any depth, of their `subCollections`."""
    out: list[str] = []
    for c in listed:
        if c.get("id"):
            out += [str(c["id"]), *collection_ids(rows(c.get("subCollections")))]
    return list(dict.fromkeys(out))


# ---- the MCP server --------------------------------------------------------------------------


class Mcp:
    """One person's session at the MCP server: `initialize`, then tool calls with their token.
    `deadline`: monotonic time; every request ends before it, minus the margin."""

    def __init__(
        self,
        source: Source,
        token: str,
        transport: httpx.AsyncBaseTransport | None,
        *,
        limit: bool = False,
    ) -> None:
        self.source, self.token, self.limit, self.n = source, token, limit, 0
        self.workspace = str(source.config.get("workspace") or "")
        self.http = httpx.AsyncClient(
            timeout=60, transport=transport or GuardedTransport(source.private_hosts)
        )
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }

    async def __aenter__(self) -> Self:
        await self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "Daimon", "version": "1"},
            },
        )
        await self.rpc("notifications/initialized", None, notify=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    async def rpc(self, method: str, params: Json | None, notify: bool = False) -> Json:
        msg: Json = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        wait = 60.0
        if self.limit and self.source.deadline is not None:
            wait = min(wait, self.source.deadline - MARGIN - time.monotonic())
            if wait <= 0:
                raise OutOfTime("out_of_time")
        await self.source.budget.take()
        try:
            r = await self.http.post(MCP, headers=self.headers, json=msg, timeout=wait)
        except httpx.HTTPError:
            raise BluedotError("unreachable") from None
        if r.status_code == 404:
            raise NotFound("http_404")
        if r.status_code >= 400:
            raise BluedotError(f"http_{r.status_code}")
        if sid := r.headers.get("mcp-session-id"):
            self.headers["Mcp-Session-Id"] = sid
        if notify or not r.content:
            return {}
        text = r.text
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            text = next((x[5:] for x in reversed(text.splitlines()) if x.startswith("data:")), "{}")
        try:
            body = obj(json.loads(text or "{}"))
        except ValueError:
            raise BluedotError("malformed") from None
        if body.get("error"):
            raise BluedotError("rpc_error")
        return obj(body.get("result"))

    async def call(self, tool: str, args: Json | None = None) -> Json:
        result = await self.rpc("tools/call", {"name": tool, "arguments": args or {}})
        out: object = result.get("structuredContent")
        if out is None:
            out = {}
            for x in rows(result.get("content")):
                text = str(x.get("text", ""))
                if x.get("type") == "text" and text[:1] in "{[":
                    try:
                        out = json.loads(text)
                    except ValueError:
                        raise BluedotError("malformed") from None
                    break
        if result.get("isError"):
            text = " ".join(str(x.get("text", "")) for x in rows(result.get("content")))
            if re.search(r"not.?found|404|no such", text, re.I):
                raise NotFound("not_found")
            raise BluedotError("tool_error")
        return obj(out)

    async def collections(self) -> list[Json]:
        """Every collection of the workspace this person lists: one page, and a missing list
        is a failure, never an empty one."""
        page = await self.call("list_collections", self._base())
        if not isinstance(page.get("collections"), list):
            raise BluedotError("bad_page")
        return rows(page["collections"])

    async def meetings(self) -> list[Json]:
        """Every meeting this person lists, page by page: a complete list, or an exception."""
        out: list[Json] = []
        for number in range(1, 10_001):
            page = await self.call(
                "list_meetings", {**self._base(), "pageNumber": number, "pageSize": PAGE}
            )
            more = obj(page.get("pagination")).get("hasNext")
            if not isinstance(page.get("items"), list) or not isinstance(more, bool):
                raise BluedotError("bad_page")
            out += rows(page["items"])
            if not more:
                return out
        raise BluedotError("endless_list")

    def _base(self) -> Json:
        return {"workspaceId": self.workspace} if self.workspace else {}


# ---- who reads a meeting ---------------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """A meeting as one person's list shows it; the uploader is read for the private ones
    only."""

    id: str
    collection: str | None
    tenancy: str
    uploader: str | None

    @property
    def private(self) -> bool:
        return self.tenancy == "user"


@dataclass
class PersonList:
    """One person's completed list: their collections and meetings."""

    person: Person
    collections: list[str]
    meetings: dict[str, Listed]
    details: dict[str, Json] = field(default_factory=dict[str, Json])
    """What `get_meeting` said of the meetings read to learn who may read them."""


def agrees(rows_: list[Listed]) -> bool:
    """Whether every list that shows a meeting says the same of where it sits and, for a
    private one, who uploaded it."""
    first = rows_[0]
    return all(
        (r.collection, r.tenancy) == (first.collection, first.tenancy)
        and (not first.private or r.uploader == first.uploader)
        for r in rows_
    )


def acl_of(ws: str, listers: list[tuple[Person, Listed]]) -> Acl:
    """Who may read a meeting: its collection's group, or the people who list it."""
    first = listers[0][1]
    if first.collection and not first.private:
        return Acl((AclRef(subject(PROVIDER, ws, "collection", first.collection), group=True),))
    refs: dict[str, AclRef] = {}
    for p, _ in listers:
        unit = ("list", f"person:{p.account_id}")
        mine = subject(PROVIDER, ws, "user", p.subject)
        refs.setdefault(mine, AclRef(mine, unit=unit))
    return Acl(tuple(refs[k] for k in sorted(refs)), personal=first.private)


def signature(a: Acl) -> str:
    """Of the access list: a change of it makes the meeting be read again."""
    key = [sorted(r.ref for r in a.refs), a.personal]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


@dataclass
class Lists:
    """What one run read of the people's lists."""

    people: list[PersonList] = field(default_factory=list[PersonList])
    ended: list[Person] = field(default_factory=list[Person])
    """People whose sign-in no longer proves itself."""
    complete: bool = True
    """Every sign-in listed completely, and at least one."""
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    sessions: dict[str, Mcp] = field(default_factory=dict[str, Mcp])


# ---- the connector ---------------------------------------------------------------------------


def workspace_of(source: Source) -> str:
    return str(source.config.get("workspace") or source.id)


class BluedotConnector(Connector):
    kind: ClassVar[str] = "bluedot"
    name: ClassVar[str] = "Bluedot"
    category: ClassVar[str] = "meetings"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    person_sign_in: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (
        Field("workspace", "Workspace ID", required=False),
        Field(
            "domain", "Email domain to trust for linking", required=False, placeholder="example.com"
        ),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport
        self._lists: dict[object, Lists] = {}

    async def release(self, source: Source) -> None:
        if (lists := self._lists.pop(source.id, None)) is not None:
            await lists.stack.aclose()

    async def test(self, source: Source) -> tuple[bool, str]:
        n = sum(p.token is not None for p in source.people)
        return (True, f"{n} people signed in") if n else (False, "Nobody has signed in yet")

    # ---- the lists ----

    async def _read(self, source: Source) -> Lists:
        """Every signed-in person's list, once per run (the pass reads it; content reuses it)."""
        if (got := self._lists.get(source.id)) is not None:
            return got
        out = self._lists.setdefault(source.id, Lists())
        for p in source.people:
            if p.token is None:
                out.ended.append(p)
                continue
            try:
                out.people.append(await self._list(source, p, p.token))
            except (BluedotError, httpx.HTTPError) as e:
                out.complete = False
                log.info("bluedot.list_failed", person=str(p.account_id), reason=str(e))
        out.complete = out.complete and bool(out.people)
        return out

    async def _list(self, source: Source, p: Person, token: str) -> PersonList:
        async with Mcp(source, token, self.transport, limit=True) as mcp:
            me = await mcp.call("get_current_user")
            found = me.get("id") or obj(me.get("user")).get("id")
            if str(found) != p.subject:
                raise BluedotError("other_account")
            collections = collection_ids(await mcp.collections())
            listed: dict[str, Listed] = {}
            details: dict[str, Json] = {}
            for m in await mcp.meetings():
                mid, tenancy = str(m["id"]), str(m.get("tenancy") or "workspace")
                cid = str(m["collectionId"]) if m.get("collectionId") else None
                up = None
                if tenancy == "user" or not cid:
                    try:
                        details[mid] = await mcp.call("get_meeting", {"videoId": mid})
                    except NotFound:
                        pass
                    else:
                        up = uploader_of(details[mid])
                listed[mid] = Listed(mid, cid, tenancy, up)
        return PersonList(p, collections, listed, details)

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """A unit per person: their collections as groups holding them (`list`), or nothing
        with no lifetime once their sign-in ended."""
        ws = workspace_of(source)
        lists = await self._read(source)
        for p in lists.ended:
            yield PermissionUnit("list", f"person:{p.account_id}", 0, {})
        for pl in lists.people:
            me = subject(PROVIDER, ws, "user", pl.person.subject)
            groups = {subject(PROVIDER, ws, "collection", c): (me,) for c in pl.collections}
            yield PermissionUnit(
                "list",
                f"person:{pl.person.account_id}",
                LEASE_MINUTES,
                groups,
                time.time_ns() // 1000,
            )

    async def accounts(self, source: Source) -> Roster | None:
        """The workspace's participants, read with any signed-in person's token. None when the
        workspace is not configured or cannot be read now: known accounts stay as they are."""
        if not source.config.get("workspace"):
            return None
        domain = "@" + str(source.config.get("domain") or "").lower().lstrip("@")
        for p in source.people:
            if p.token is None:
                continue
            try:
                async with Mcp(source, p.token, self.transport) as mcp:
                    out = await mcp.call(
                        "get_workspace", {"workspaceId": str(source.config["workspace"])}
                    )
            except (BluedotError, httpx.HTTPError):
                continue
            if not isinstance(out.get("participants"), list):
                continue
            accounts: list[SourceAccount] = []
            for u in rows(out["participants"]):
                uid, email = u.get("userId") or u.get("id"), clean_email(u.get("email"))
                if uid:
                    active = u.get("active") is not False
                    trusted = bool(email and len(domain) > 1 and email.endswith(domain))
                    accounts.append(
                        SourceAccount(
                            str(uid), str(u.get("name") or ""), email, trusted and active, active
                        )
                    )
            return Roster(PROVIDER, workspace_of(source), tuple(accounts))
        return None

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        ws = workspace_of(source)
        lists = await self._read(source)
        by_meeting: dict[str, list[tuple[Person, Listed]]] = {}
        for pl in sorted(lists.people, key=lambda x: str(x.person.account_id)):
            for mid, m in pl.meetings.items():
                by_meeting.setdefault(mid, []).append((pl.person, m))
        old = obj(source.state.get("read"))
        read: Json = {k: v for k, v in old.items() if k in by_meeting}
        state: Json = {"v": 1, "read": read}
        live: set[str] = set()
        now = datetime.now(UTC)
        n = 0
        for mid in sorted(by_meeting):
            listers = by_meeting[mid]
            if not agrees([m for _, m in listers]):
                read.pop(mid, None)  # who may read it is disputed: it is not kept
                continue
            a = acl_of(ws, listers)
            prev = read.get(mid)
            had = cast("list[object]", prev) if isinstance(prev, list) else None
            sig = signature(a)
            if (
                had
                and had[0] == sig
                and (had[2] or now - (stamp(had[1]) or now) > timedelta(days=WAIT_DAYS))
            ):
                live.add(f"meeting:{mid}")
                continue
            detail = await self._open(source, lists, mid)
            if detail is None:  # refused or unreachable: nothing is kept that it would grant
                read.pop(mid, None)
                continue
            first_read = stamp(had[1]) if had else None
            rec, full = self._record(listers[0][1], detail, a, first_read or now)
            read[mid] = [sig, (first_read or now).isoformat(), full]
            live.add(f"meeting:{mid}")
            yield rec
            n += 1
            if n % BATCH == 0:
                yield Checkpoint(state)
        yield SyncState(state, live if lists.complete else None)

    async def _open(self, source: Source, lists: Lists, mid: str) -> Json | None:
        """The meeting as the first person who lists it that can still open it."""
        for pl in lists.people:
            if mid not in pl.meetings:
                continue
            if mid in pl.details:
                return pl.details[mid]
            token = pl.person.token
            if token is None:
                continue
            key = str(pl.person.account_id)
            try:
                if key not in lists.sessions:
                    lists.sessions[key] = await lists.stack.enter_async_context(
                        Mcp(source, token, self.transport)
                    )
                return await lists.sessions[key].call("get_meeting", {"videoId": mid})
            except NotFound:
                continue
            except (BluedotError, httpx.HTTPError):
                lists.sessions.pop(key, None)
                continue
        return None

    @staticmethod
    def _record(m: Listed, detail: Json, a: Acl, first_read: datetime) -> tuple[ItemRecord, bool]:
        """The record of a meeting, and whether it has both its summary and transcript. A read
        carries no collection or privacy: they come from the lists."""
        title = str(detail.get("title") or "Untitled meeting")
        started = next((t for k in TIME_KEYS if (t := meeting_time(detail.get(k)))), None)
        summary = summary_text(detail.get("summary"))
        transcript = turns(detail.get("transcript") or detail.get("transcription"))
        body = "\n\n".join(
            x
            for x in (
                summary and f"## Summary\n\n{summary}",
                transcript and f"## Transcript\n{transcript}",
            )
            if x
        )
        rec = ItemRecord(
            f"meeting:{m.id}",
            "meeting",
            title,
            body,
            started or first_read,
            container=m.collection,
            container_kind="collection",
            payload={"tenancy": m.tenancy, "collection": m.collection},
            facets={"date_basis": "source" if started else "receipt"},
            acl=a,
        )
        return rec, bool(summary and transcript)
