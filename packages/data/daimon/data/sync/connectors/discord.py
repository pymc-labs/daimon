"""Discord: each conversation window is readable by the members Discord lets read its channel.

The source's own bot token reads one server. Each run first computes, per member and per root
channel, Discord's own answer (owner, Administrator, role and member overwrites; View Channel and
Read Message History for text, Connect as well for voice) and publishes it as one permission unit:
a group per root channel and per private thread, whose members are Discord subjects. A window's
access list is the group of its root (a public thread uses its parent's, a private thread its
own). The bot needs the Server Members intent, and View Channel and Read Message History on
the channels to archive (Manage Threads to read private threads).

Messages become one record per channel or thread and UTC day (split past `WINDOW` characters).
The last `rewalk_days` are re-read once per full pass, so edits and deletes reach the store. An
excluded channel or thread loses its windows, as does a thread Discord says is gone. A channel
the bot can no longer read (or that went) publishes no readers, so its windows are hidden at
once; read again within `LOST` (30 days) they show again where they were, later they go.

Configuration (`sources.config`), credential `{"token": "<bot token>"}`:

- `workspace`: the server's numeric id. `exclude`: channel or thread ids or names to never read.
- `rewalk_days`: days re-read for edits (default 7).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Final

import httpx
import structlog
from daimon.data.access import subject
from daimon.data.identity import SourceAccount
from daimon.data.sync.attachments import BLOB_CAP
from daimon.data.sync.connector import (
    Acl,
    AclRef,
    Attachment,
    Checkpoint,
    Connector,
    Descriptor,
    Field,
    ItemRecord,
    Json,
    PermissionUnit,
    Record,
    Replaced,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import (
    REWALK_DAYS,
    Unreadable,
    authors_of,
    lost_for_good,
    names,
    obj,
    rows,
    split,
    whole_days,
)
from daimon.data.sync.http import GuardedTransport, Upstream, download, send

log = structlog.get_logger()

PROVIDER: Final = "discord"
API: Final = "https://discord.com/api/v10"
CDN: Final = ("cdn.discordapp.com", "media.discordapp.net")
EPOCH: Final = 1420070400000
"""Discord's snowflake epoch, in milliseconds."""
VIEW, ADMIN, HISTORY = 1 << 10, 1 << 3, 1 << 16
CONNECT, MANAGE_THREADS = 1 << 20, 1 << 34
READ: Final = VIEW | HISTORY
CATEGORY, FORUM, MEDIA, PRIVATE = 4, 15, 16, 12
VOICE: Final = {2, 13}
ROOTS: Final = {0, 2, 5, 13, FORUM, MEDIA}
"""Text, voice, announcement and stage chat; forum and media hold only posts (threads)."""
THREADED: Final = {0, 5, FORUM, MEDIA}
THREADS: Final = {10, 11, PRIVATE}
KEEP: Final = {0, 19}
"""Default messages and replies; joins, pins and other system messages are left out."""
RELIST: Final = timedelta(days=7)
"""How often a channel's archived threads are all listed again (to notice deleted ones)."""
UNKNOWN_CHANNEL: Final = 10003
PASS_MINUTES: Final = 60


# ---- Discord's permission rules --------------------------------------------------------------


@dataclass(frozen=True)
class Guild:
    """The server's permission facts: owner, role bits, and each member's role ids."""

    id: str
    owner: str | None
    roles: dict[str, int]
    members: dict[str, list[str]]


def permissions(g: Guild, ch: Json, roles: Sequence[str], uid: str | None = None) -> int:
    """Discord's order: base roles, then the channel's @everyone, role and member overwrites.
    An Administrator holds every bit (-1)."""
    perms = g.roles.get(g.id, 0)
    for r in roles:
        perms |= g.roles.get(r, 0)
    if perms & ADMIN:
        return -1
    ow = {o["id"]: (int(o["allow"]), int(o["deny"])) for o in rows(ch.get("permission_overwrites"))}
    if e := ow.get(g.id):
        perms = perms & ~e[1] | e[0]
    allow = deny = 0
    for r in roles:
        if o := ow.get(r):
            allow, deny = allow | o[0], deny | o[1]
    perms = perms & ~deny | allow
    if uid and (m := ow.get(uid)):
        perms = perms & ~m[1] | m[0]
    return perms


def holds(g: Guild, ch: Json, uid: str, bits: int) -> bool:
    """Whether a member has every one of `bits` on a channel (owner and Administrators: all)."""
    return uid == g.owner or permissions(g, ch, g.members[uid], uid) & bits == bits


def needs(ch: Json) -> int:
    """What a member must hold on a root to read it; voice and stage text also need Connect."""
    return READ | (CONNECT if ch.get("type") in VOICE else 0)


def hidden(exclude: frozenset[str], obj: Json) -> bool:
    return bool({str(obj["id"]), str(obj.get("name") or "").lower()} & exclude)


def snowflake(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000 - EPOCH) << 22


def stamp(sf: str | int) -> datetime:
    return datetime.fromtimestamp(((int(sf) >> 22) + EPOCH) / 1000, UTC)


# ---- configuration and the server as one run reads it ----------------------------------------


@dataclass(frozen=True)
class Config:
    guild: str
    exclude: frozenset[str]
    days: int

    @classmethod
    def of(cls, config: Json) -> Config:
        guild = str(config.get("workspace") or "").strip()
        if not guild.isdecimal():
            raise ValueError("config.workspace must be the server's numeric id")
        return cls(
            guild,
            frozenset(x.lower() for x in names(config, "exclude")),
            whole_days(config, REWALK_DAYS),
        )


@dataclass
class World:
    """The server as one run reads it: permissions of every human, the channels, and the private
    threads the bot may list."""

    guild: Guild
    name: str
    bot: str
    humans: dict[str, str]
    channels: dict[str, Json]
    roles: dict[str, str]
    private: dict[str, Json] = field(default_factory=dict[str, Json])
    thread_members: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def bot_holds(self, ch: Json, bits: int) -> bool:
        return holds(self.guild, ch, self.bot, bits)

    def readers(self, ch: Json) -> list[str]:
        return sorted(u for u in self.humans if holds(self.guild, ch, u, needs(ch)))

    def private_readers(self, thread: Json) -> list[str]:
        """Thread members who read its parent, and the parent's readers who manage threads."""
        parent = self.channels[thread["parent_id"]]
        on_parent = set(self.readers(parent))
        managers = {u for u in on_parent if holds(self.guild, parent, u, MANAGE_THREADS)}
        members = set(self.thread_members.get(thread["id"], ())) | managers
        return sorted(on_parent & members)

    def access(self, exclude: frozenset[str]) -> dict[str, list[str]]:
        """Group id (a root the bot reads, or a private thread) to the humans who read it."""
        out = {
            cid: self.readers(ch)
            for cid, ch in self.channels.items()
            if ch["type"] in ROOTS and not hidden(exclude, ch) and self.bot_holds(ch, READ)
        }
        for tid, t in self.private.items():
            if t.get("parent_id") in out and not hidden(exclude, t):
                out[tid] = self.private_readers(t)
        return out


@dataclass
class Run:
    """One run's reading position; `state` is what the cursor keeps."""

    guild: str
    rewalk: date
    cycle: int
    state: Json
    exclude: frozenset[str]
    names: dict[str, str] = field(default_factory=dict[str, str])
    vis: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def streams(self) -> dict[str, list[Any]]:
        """Stream id to `[after, day, seen, cycle, root]`: days before `day` are stored."""
        return self.state["streams"]

    @property
    def listed(self) -> dict[str, Json]:
        """Root id to `{at, full}`: when its threads were last read, and all listed."""
        return self.state["listed"]

    def move(self, sid: str, cursor: list[Any]) -> Checkpoint:
        self.streams[sid] = cursor
        return Checkpoint(self.state)


def root_of(sid: str, cursor: list[Any]) -> str:
    return cursor[4] if len(cursor) > 4 else sid


class Api:
    """GET calls to the server inside the source's budget."""

    def __init__(self, client: httpx.AsyncClient, source: Source) -> None:
        self.client, self.source = client, source

    async def get(self, path: str, soft: bool = False, **params: object) -> Any:  # noqa: ANN401
        """The JSON answer. `soft`: a refusal (403, 404, wrong channel type) raises `Unreadable`."""
        r = await send(self.client, self.source.budget, path, params, deadline=self.source.deadline)
        code = _code(r)
        if soft and (r.status_code in (403, 404) or r.status_code == 400 and code == 50024):
            raise Unreadable(f"{path}: HTTP {r.status_code}", code)
        if r.status_code == 401:
            raise Upstream("Discord rejected the bot token")
        if r.status_code >= 400:
            raise Upstream(f"Discord answered HTTP {r.status_code} for {path}")
        return r.json()


def _code(r: httpx.Response) -> int | None:
    try:
        return r.json().get("code")
    except (ValueError, AttributeError):
        return None


async def read_world(api: Api, cfg: Config) -> World:
    """Server, channels and members, then the private threads the bot may list and their members."""
    me = await api.get("/users/@me")
    raw = await api.get(f"/guilds/{cfg.guild}")
    channels = {c["id"]: c for c in await api.get(f"/guilds/{cfg.guild}/channels")}
    members: dict[str, list[str]] = {}
    humans: dict[str, str] = {}
    after = "0"
    while True:
        try:
            page = await api.get(f"/guilds/{cfg.guild}/members", True, limit=1000, after=after)
        except Unreadable:
            raise Upstream(
                "the bot needs the Server Members intent and to be in the server"
            ) from None
        for m in page:
            u = m["user"]
            members[u["id"]] = m.get("roles", [])
            if not u.get("bot"):
                humans[u["id"]] = u.get("global_name") or u.get("username") or ""
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]
    bot = str(me["id"])
    if bot not in members:
        raise Upstream("the bot is not a member of the server")
    roles = {r["id"]: r for r in raw.get("roles", [])}
    guild = Guild(
        cfg.guild,
        raw.get("owner_id"),
        {k: int(r["permissions"]) for k, r in roles.items()},
        members,
    )
    world = World(
        guild,
        raw.get("name") or cfg.guild,
        bot,
        humans,
        channels,
        {k: r["name"] for k, r in roles.items()},
    )
    await private_threads(api, world, cfg)
    return world


async def private_threads(api: Api, world: World, cfg: Config) -> None:
    """Only routes the bot's computed permissions allow are called: a 403 counts toward Discord's
    invalid-request ban."""
    active = (await api.get(f"/guilds/{cfg.guild}/threads/active")).get("threads", [])
    found = {t["id"]: t for t in active if t.get("type") == PRIVATE}
    for rid, ch in world.channels.items():
        if ch["type"] not in THREADED or hidden(cfg.exclude, ch):
            continue
        if not world.bot_holds(ch, HISTORY | MANAGE_THREADS | VIEW):
            continue
        try:
            found |= (await list_archived(api, ch, "private", None))[0]
        except Unreadable as e:
            log.info("discord.private_threads_unreadable", channel=rid, reason=str(e))
    world.private = {
        tid: t
        for tid, t in found.items()
        if t.get("parent_id") in world.channels and not hidden(cfg.exclude, t)
    }
    for tid in sorted(world.private):
        try:
            rows = await api.get(f"/channels/{tid}/thread-members", True)
        except Unreadable:
            del world.private[tid]  # members unknown: nobody reads it
            continue
        world.thread_members[tid] = [str(m["user_id"]) for m in rows]


async def list_archived(
    api: Api, ch: Json, kind: str, stop: datetime | None
) -> tuple[dict[str, Json], bool]:
    """Archived threads of a root ('public' or 'private'), newest first until `stop`; False when
    the bot cannot list them."""
    found: dict[str, Json] = {}
    before = None
    try:
        while True:
            page = await api.get(
                f"/channels/{ch['id']}/threads/archived/{kind}", True, limit=100, before=before
            )
            at = [
                (t, datetime.fromisoformat(t["thread_metadata"]["archive_timestamp"]))
                for t in page.get("threads", [])
            ]
            found |= {
                t["id"]: t
                for t, when in at
                if t.get("type") in THREADS and not (stop and when < stop)
            }
            if not page.get("has_more") or not at or stop and at[-1][1] < stop:
                return found, True
            before = at[-1][0]["thread_metadata"]["archive_timestamp"]
    except Unreadable as e:
        log.info("discord.archived_unreadable", channel=ch["id"], kind=kind, reason=str(e))
        return found, False


def digest(users: list[str]) -> str:
    return hashlib.sha256(",".join(users).encode()).hexdigest()[:16]


def group_of(guild: str, group: str) -> str:
    return subject(PROVIDER, guild, "channel", group)


# ---- the connector --------------------------------------------------------------------------


class DiscordConnector(Connector):
    kind: ClassVar[str] = "discord"
    name: ClassVar[str] = "Discord"
    category: ClassVar[str] = "chat"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Bot token", "secret"),
        Field("workspace", "Server ID"),
        Field("exclude", "Never read", "list", required=False, placeholder="channel IDs or names"),
        Field("rewalk_days", "Days re-read for edits", required=False, placeholder="7"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport
        self._worlds: dict[object, World] = {}

    def _client(self, source: Source) -> httpx.AsyncClient:
        token = source.credentials.get("token")
        if not token:
            raise ValueError("the source has no bot token")
        return httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bot {token}"},
            timeout=30,
            transport=self.transport or GuardedTransport(source.private_hosts),
        )

    async def _world(self, source: Source, cfg: Config) -> World:
        """The server, read once per run (the pass reads it; accounts and content reuse it)."""
        if (world := self._worlds.get(source.id)) is None:
            async with self._client(source) as client:
                world = self._worlds[source.id] = await read_world(Api(client, source), cfg)
        return world

    async def release(self, source: Source) -> None:
        self._worlds.pop(source.id, None)

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            cfg = Config.of(source.config)
            async with self._client(source) as client:
                world = await read_world(Api(client, source), cfg)
        except (ValueError, Upstream, Unreadable) as e:
            return False, str(e)
        roots = [
            c for c in world.channels.values() if c["type"] in ROOTS and not hidden(cfg.exclude, c)
        ]
        readable = sum(world.bot_holds(c, needs(c)) for c in roots)
        return True, f"{world.name}: {readable} of {len(roots)} channels readable"

    # ---- who reads what ----

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit, complete every run: a group per root and per private thread (a group missing
        from the batch loses all its members)."""
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        groups = {
            group_of(cfg.guild, cid): tuple(subject(PROVIDER, cfg.guild, "user", u) for u in users)
            for cid, users in world.access(cfg.exclude).items()
        }
        yield PermissionUnit(
            "pass", f"{cfg.guild}:channels", PASS_MINUTES, groups, time.time_ns() // 1000
        )

    async def accounts(self, source: Source) -> Roster | None:
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        people = tuple(SourceAccount(uid, name) for uid, name in sorted(world.humans.items()))
        return Roster(PROVIDER, cfg.guild, people)

    # ---- content ----

    def _layout(
        self, source: Source, cfg: Config, world: World
    ) -> tuple[Run, dict[str, tuple[Json, Json]], list[str]]:
        """The run's position, the roots the bot reads with their facets, and the streams whose
        windows go: excluded ones, and those of a root lost for good."""
        old = source.state
        kept = {
            k
            for k, c in world.channels.items()
            if c["type"] in ROOTS and not hidden(cfg.exclude, c)
        }
        roots: dict[str, tuple[Json, Json]] = {}
        for ch in sorted(world.channels.values(), key=lambda c: int(c["id"])):
            if ch["id"] in kept and world.bot_holds(ch, READ):
                parent = world.channels.get(str(ch.get("parent_id"))) or {}
                facets = {"channel_id": ch["id"], "category": parent.get("name")}
                roots[ch["id"]] = (ch, {k: v for k, v in facets.items() if v})
        same = old.get("exclude", []) == sorted(cfg.exclude)
        state: Json = {
            "v": 1,
            "cycle": old.get("cycle", 0) + (old.get("complete") is not False),
            "complete": False,
            "exclude": sorted(cfg.exclude),
            "streams": dict(obj(old.get("streams"))),
            "listed": {k: v for k, v in obj(old.get("listed")).items() if k in roots and same},
            "lost": dict(obj(old.get("lost"))),
        }
        run = Run(
            cfg.guild,
            datetime.now(UTC).date() - timedelta(days=cfg.days),
            state["cycle"],
            state,
            cfg.exclude,
        )
        run.names = {**{k: c.get("name") or k for k, c in world.channels.items()}, **world.roles}
        run.vis = {k: digest(v) for k, v in world.access(cfg.exclude).items()}

        def excluded(s: str, cur: list[Any]) -> bool:
            root = world.channels.get(root_of(s, cur))
            return s.lower() in cfg.exclude or (root is not None and hidden(cfg.exclude, root))

        gone = [s for s, cur in run.streams.items() if excluded(s, cur)]
        missing = {root_of(s, c) for s, c in run.streams.items() if s not in gone} - set(roots)
        lost = lost_for_good(state["lost"], missing, datetime.now(UTC))
        gone += [s for s, cur in run.streams.items() if root_of(s, cur) in lost]
        return run, roots, gone

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        run, roots, gone = self._layout(source, cfg, world)
        for sid in gone:
            yield Replaced(f"conversation:{sid}:", "")
            run.streams.pop(sid, None)
        async with self._client(source) as client:
            api = Api(client, source)
            active = (await api.get(f"/guilds/{cfg.guild}/threads/active")).get("threads", [])
            for rid, (ch, facets) in roots.items():
                mine = [t for t in active if t.get("parent_id") == rid]
                async for rec in self._root(api, run, world, ch, facets, mine):
                    yield rec
                yield Checkpoint(run.state)
        run.state["complete"] = True
        yield SyncState(run.state)

    async def _root(
        self, api: Api, run: Run, world: World, ch: Json, facets: Json, active: list[Json]
    ) -> AsyncGenerator[Record]:
        """A channel, then its threads: active ones and those archived since its threads were last
        all read (all of them every `RELIST`)."""
        now = datetime.now(UTC)
        if ch["type"] not in (FORUM, MEDIA):
            async for rec in self._stream(
                api, run, world, ch["id"], ch, None, ch.get("last_message_id"), facets
            ):
                yield rec
        threads = {t["id"]: t for t in active if t.get("type") in THREADS}
        seen = run.listed.get(ch["id"]) or {}
        full = not seen.get("full") or now - datetime.fromisoformat(seen["full"]) > RELIST
        floor = datetime(run.rewalk.year, run.rewalk.month, run.rewalk.day, tzinfo=UTC)
        stop = None if full else min(datetime.fromisoformat(seen["at"]), floor)
        listed = True
        if ch["type"] in THREADED:
            found, listed = await list_archived(api, ch, "public", stop)
            threads |= found
            if world.bot_holds(ch, HISTORY | MANAGE_THREADS):
                threads |= {
                    t["id"]: t for t in world.private.values() if t["parent_id"] == ch["id"]
                }
        for t in sorted(threads.values(), key=lambda t: int(t["id"])):
            if t.get("type") == PRIVATE and t["id"] not in world.private:
                continue  # its members are unknown: nobody reads it
            if hidden(run.exclude, t):
                yield self._forget(run, t["id"])
            else:
                async for rec in self._stream(
                    api, run, world, t["id"], ch, t, t.get("last_message_id"), facets
                ):
                    yield rec
        if listed and full:
            for sid, cur in list(run.streams.items()):
                if (
                    root_of(sid, cur) == ch["id"]
                    and sid != ch["id"]
                    and sid not in threads
                    and await self._deleted(api, sid)
                ):
                    yield self._forget(run, sid)
        if listed:
            run.listed[ch["id"]] = {
                "at": now.isoformat(),
                "full": now.isoformat() if full else seen["full"],
            }

    @staticmethod
    def _forget(run: Run, sid: str) -> Replaced:
        run.streams.pop(sid, None)
        return Replaced(f"conversation:{sid}:", "")

    @staticmethod
    async def _deleted(api: Api, sid: str) -> bool:
        """Whether Discord says a thread it no longer lists does not exist. Any other refusal
        (a private thread the bot cannot see) leaves it as stored."""
        try:
            await api.get(f"/channels/{sid}", True)
        except Unreadable as e:
            return e.code == UNKNOWN_CHANNEL
        return False

    async def _stream(
        self,
        api: Api,
        run: Run,
        world: World,
        sid: str,
        ch: Json,
        thread: Json | None,
        last: str | None,
        facets: Json,
    ) -> AsyncGenerator[Record]:
        """One channel or thread, oldest first from its cursor; the days in the re-walk window are
        rebuilt whole, each day's deletes applied as it completes. A room the bot cannot read
        after all is logged and left as it is."""
        try:
            async for rec in self._read(api, run, sid, ch, thread, last, facets, world):
                yield rec
        except Unreadable as e:
            log.info("discord.stream_unreadable", room=sid, reason=str(e))

    async def _read(
        self,
        api: Api,
        run: Run,
        sid: str,
        ch: Json,
        thread: Json | None,
        last: str | None,
        facets: Json,
        world: World,
    ) -> AsyncGenerator[Record]:
        cur = run.streams.get(sid)
        after, day, seen, cycle = (
            (int(cur[0]), cur[1], int(cur[2]), cur[3]) if cur else (0, "", 0, None)
        )
        if cur and day >= run.rewalk.isoformat() and cycle != run.cycle:
            after, day = snowflake(run.rewalk) - 1, run.rewalk.isoformat()
        elif cur and not (last and int(last) > seen):
            return  # nothing new, nothing left to re-read
        group = sid if thread and thread.get("type") == PRIVATE else ch["id"]
        prefix = f"conversation:{sid}:"
        msgs: list[Json] = []
        d: str | None = None
        done, start = after, day

        def emit(upto: str | None) -> list[Record]:
            records = (
                self._windows(run, world, sid, ch, thread, group, d or "", msgs, facets)
                if msgs
                else []
            )
            keep = {r.external_id for r in records}
            return [*records, Replaced(prefix, start, upto, keep)]

        async for m in self._messages(api, sid, after):
            md = stamp(m["id"]).date().isoformat()
            if md != d:
                if msgs:
                    for rec in emit(md):
                        yield rec
                    done, start = int(msgs[-1]["id"]), md
                    yield run.move(sid, [str(done), md, str(done), run.cycle, ch["id"]])
                d, msgs = md, []
            msgs.append(m)
        if cur and d is None:  # an empty answer where days are stored: suspicious, delete nothing
            raise Unreadable("no messages where some were stored; left as it is")
        for rec in emit(None):
            yield rec
        yield run.move(
            sid, [str(done), d or day, msgs[-1]["id"] if msgs else "0", run.cycle, ch["id"]]
        )

    @staticmethod
    async def _messages(api: Api, sid: str, after: int) -> AsyncGenerator[Json]:
        while True:
            page = sorted(
                await api.get(f"/channels/{sid}/messages", True, after=str(after), limit=100),
                key=lambda m: int(m["id"]),
            )
            for m in page:
                yield m
            if len(page) < 100:
                return
            after = int(page[-1]["id"])

    @staticmethod
    def _text(run: Run, m: Json, vis: str | None) -> str:
        """The message with mentions as names; a channel is named only where its readers are the
        window's own, so a private name never reaches another audience."""
        users = {
            u["id"]: u.get("global_name") or u.get("username") for u in rows(m.get("mentions"))
        }
        t = m.get("content") or ""
        t = re.sub(r"<@!?(\d+)>", lambda x: "@" + str(users.get(x[1]) or "someone"), t)
        t = re.sub(r"<@&(\d+)>", lambda x: "@" + str(run.names.get(x[1]) or "role"), t)

        def channel(x: re.Match[str]) -> str:
            shown = run.vis.get(x[1]) == vis and vis is not None
            return "#" + ((run.names.get(x[1]) or "channel") if shown else "channel")

        t = re.sub(r"<#(\d+)>", channel, t)
        t = re.sub(r"<a?:(\w+):\d+>", r":\1:", t)
        t = re.sub(r"\s*\n\s*", " / ", t.strip())
        files = " ".join(f"[file: {a.get('filename')}]" for a in rows(m.get("attachments")))
        return " ".join(x for x in (t, files) if x)

    def _windows(
        self,
        run: Run,
        world: World,
        sid: str,
        ch: Json,
        thread: Json | None,
        group: str,
        day: str,
        msgs: list[Json],
        facets: Json,
    ) -> list[ItemRecord]:
        """One day as `HH:MM Author: text` lines, split past `WINDOW` characters."""
        vis = run.vis.get(group)
        lines: list[str] = []
        ids: list[str] = []
        authors: list[str] = []
        files: list[list[Attachment]] = []
        for m in msgs:
            a = obj(m.get("author"))
            if a.get("bot") or m.get("webhook_id") or m.get("type", 0) not in KEEP:
                continue
            if not (text := self._text(run, m, vis)):
                continue
            who = a.get("global_name") or a.get("username") or "unknown"
            ref = obj(obj(m.get("referenced_message")).get("author"))
            to = f" (to {ref.get('global_name') or ref.get('username')})" if ref else ""
            lines.append(f"{stamp(m['id']):%H:%M} {who}{to}: {text}")
            ids.append(m["id"])
            authors.append(who)
            files.append(
                [
                    Attachment(
                        f["filename"], f.get("content_type") or "", None, int(f.get("size") or 0)
                    )
                    for f in rows(m.get("attachments"))
                ]
            )
        name = f"#{ch.get('name')}" + (f" › {thread.get('name')}" if thread else "")
        access = Acl((AclRef(group_of(run.guild, group), group=True),))
        parts = split(lines)
        out: list[ItemRecord] = []
        for k, part in enumerate(parts, 1):
            who = list(dict.fromkeys(authors[i] for i in part))
            first = ids[part[0]]
            out.append(
                ItemRecord(
                    external_id=f"conversation:{sid}:{day}" + (f":{k}" if k > 1 else ""),
                    kind="conversation",
                    title=f"{name} · {day}" + (f" ({k}/{len(parts)})" if len(parts) > 1 else ""),
                    body="\n".join(lines[i] for i in part),
                    occurred_at=stamp(first),
                    container=ch.get("name"),
                    author=authors_of(who),
                    url=f"https://discord.com/channels/{run.guild}/{sid}/{first}",
                    payload={
                        "channel_id": ch["id"],
                        **({"thread_id": sid} if thread else {}),
                        "first": first,
                        "last": ids[part[-1]],
                        "authors": who,
                        "lines": [ids[i] for i in part],
                    },
                    facets={**facets, **({"thread_id": sid} if thread else {})},
                    acl=access,
                    files=[f for i in part for f in files[i]],
                )
            )
        return out

    async def fetch_file(
        self, source: Source, payload: Json, message: str, name: str, index: int = 0
    ) -> tuple[bytes, str] | None:
        """The message read now in its own room (a thread's line in the thread) for a fresh link,
        then the attachment of that file name."""
        room = str(payload.get("thread_id") or payload.get("channel_id") or "")
        if not room.isdecimal() or not str(message).isdecimal():
            return None
        async with self._client(source) as client:
            try:
                m = await Api(client, source).get(f"/channels/{room}/messages/{message}", True)
            except Unreadable:
                return None
        named = [a for a in rows(m.get("attachments")) if a.get("filename") == name]
        if index >= len(named) or int(named[index].get("size") or 0) > BLOB_CAP:
            return None
        found = named[index]
        data = await download(
            self.transport or GuardedTransport(source.private_hosts),
            found.get("url"),
            CDN,
            BLOB_CAP,
        )
        return (
            None
            if data is None
            else (data, found.get("content_type") or "application/octet-stream")
        )
