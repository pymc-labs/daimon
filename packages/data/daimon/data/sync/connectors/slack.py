"""Slack: every conversation window is readable by the people Slack itself lets read its channel.

Public channels are read (with a bot token, those the bot is in; with a user token, all). A bot
token also reads the private channels it is in, unless `private` is `no`. Direct messages and group
direct messages are never read. Each run computes Slack's own answer (a public channel is read
by every active full member of the workspace and the active guests in it, a private one by its
members among them; never bots, deactivated or invited-only users, or Slack Connect users of
other organizations) and publishes it as one permission unit: a group per channel whose members
are Slack subjects. A window's access list is its channel's group (threads too). Any failure to
read the workspace raises: nothing is published and the last pass expires within the hour.

A channel is one record per UTC day of top-level messages; a thread's replies are records of
their own, one per day of reply. The last `rewalk_days` are re-read on every run (edits, deletes
and new replies). An excluded channel loses its windows. A channel the bot no longer reads
publishes no readers, so its windows are hidden at once; read again within `LOST` (30 days)
they show again where they were, later they go.

Scopes: `channels:read`, `channels:history`, `users:read`; for private channels also
`groups:read` and `groups:history`; `files:read` to open attachments. `users:read.email` is
optional: with it, confirmed emails are kept for linking accounts.

Configuration (`sources.config`), credential `{"token": "xoxb-..." or "xoxp-..."}`:

- `workspace`: the workspace's team id (`T...`); the token must read that workspace.
- `private`: `no` to leave out the private channels the bot is in (bot tokens only). `exclude`:
  channel ids or names to never read. `rewalk_days`: days re-read for edits and replies (7).
"""

from __future__ import annotations

import html
import math
import re
import time
from collections.abc import AsyncGenerator
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
    switch,
    whole_days,
)
from daimon.data.sync.http import GuardedTransport, Upstream, download, send

log = structlog.get_logger()

PROVIDER: Final = "slack"
API: Final = "https://slack.com/api"
FILES: Final = ("files.slack.com",)
"""The only host a file link may point to: the token goes nowhere else."""
PAGE: Final = 200
SPAN: Final = 30
"""Days read at once in a busy channel: bounds memory."""
KEEP: Final = {None, "thread_broadcast", "file_share", "me_message"}
"""Message subtypes kept; joins, topics and other system messages are left out."""
AUTH: Final = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
UNREADABLE: Final = {
    "channel_not_found",
    "not_in_channel",
    "is_archived",
    "access_denied",
    "thread_not_found",
}
PASS_MINUTES: Final = 60


@dataclass(frozen=True)
class Config:
    team: str
    private: bool
    exclude: frozenset[str]
    days: int

    @classmethod
    def of(cls, config: Json) -> Config:
        team = str(config.get("workspace") or "").strip()
        if not team:
            raise ValueError("config.workspace must be the workspace's team id")
        return cls(
            team,
            switch(config, "private"),
            frozenset(x.lower() for x in names(config, "exclude")),
            whole_days(config, REWALK_DAYS),
        )


def token_of(source: Source) -> str:
    token = source.credentials.get("token", "")
    if not token.startswith(("xoxb-", "xoxp-")):
        raise ValueError("use a bot token (xoxb-) or a user token (xoxp-)")
    return token


def is_human(u: Json, team: str) -> bool:
    """A person of this workspace, deactivated or not: no bot, app, Slackbot or outsider."""
    bots = u.get("is_bot") or u.get("is_app_user") or u.get("is_invited_user")
    return not (bots or u.get("id") == "USLACKBOT") and u.get("team_id") == team


def is_guest(u: Json) -> bool:
    return bool(u.get("is_restricted") or u.get("is_ultra_restricted"))


def wanted(ch: Json, exclude: frozenset[str], joined: bool, private: bool) -> bool:
    """A public channel not left out (and, with `joined`, one the bot is in), or with `private` a
    private one the bot is in: never a direct message or group direct message."""
    if ch.get("is_im") or ch.get("is_mpim"):
        return False
    if {ch["id"].lower(), str(ch.get("name") or "").lower()} & exclude:
        return False
    if ch.get("is_private"):
        return private and bool(ch.get("is_member"))
    return bool(ch.get("is_member")) or not joined


def stamp(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


def epoch(d: date) -> str:
    return f"{datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp():.6f}"


class MissingScope(Upstream):
    """The token lacks a scope the call needs."""


class Api:
    """Method calls inside the source's budget."""

    def __init__(self, client: httpx.AsyncClient, source: Source) -> None:
        self.client, self.source = client, source

    async def call(self, method: str, soft: bool = False, **params: object) -> Json:
        """One method's answer. `soft`: errors meaning the bot may not read it raise Unreadable."""
        r = await send(
            self.client, self.source.budget, f"/{method}", params, deadline=self.source.deadline
        )
        if r.status_code >= 400:
            raise Upstream(f"Slack answered HTTP {r.status_code} for {method}")
        j: Json = r.json()
        if j.get("ok"):
            return j
        err = j.get("error", "unknown_error")
        if soft and err in UNREADABLE:
            raise Unreadable(f"{method}: {err}")
        if err in AUTH:
            raise Upstream(f"Slack rejected the token ({err})")
        if err == "missing_scope":
            needed = j.get("needed", "required")
            raise MissingScope(f"the Slack token is missing the {needed} scope")
        raise Upstream(f"Slack: {err}")

    async def pages(
        self, method: str, key: str, soft: bool = False, **params: object
    ) -> list[Json]:
        out: list[Json] = []
        cursor = None
        while True:
            j = await self.call(method, soft, cursor=cursor, **params)
            out += j.get(key, [])
            cursor = obj(j.get("response_metadata")).get("next_cursor")
            if not cursor:
                return out


@dataclass
class World:
    """The workspace as one run reads it: our people, and the channels with their readers."""

    team: str
    name: str
    url: str
    channels: dict[str, Json]
    full: frozenset[str]
    guests: dict[str, frozenset[str]]
    """Channel id to its members who are active guests."""
    people: dict[str, Json]
    """Every human of ours, deactivated included."""
    user_token: bool
    private: bool
    warning: str | None = None
    """What the run could not read, for the source's status."""
    inside: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    """Private channel id to its members among our active humans."""

    def roots(self, exclude: frozenset[str]) -> dict[str, Json]:
        return {
            cid: ch
            for cid, ch in self.channels.items()
            if wanted(ch, exclude, joined=not self.user_token, private=self.private)
        }

    def access(self, exclude: frozenset[str]) -> dict[str, list[str]]:
        """Channel id to the people who read it, for every channel read (none: an empty list)."""
        return {
            cid: sorted(
                self.inside[cid]
                if ch.get("is_private")
                else self.full | self.guests.get(cid, frozenset())
            )
            for cid, ch in self.roots(exclude).items()
        }


async def read_world(api: Api, cfg: Config, token: str) -> World:
    """Channels and users, then the members of each private channel and of each public one where
    guests exist. A failed call raises."""
    me = await api.call("auth.test")
    if str(me.get("team_id") or "") != cfg.team:
        raise Upstream("the token reads another workspace than the declared one")
    user_token = token.startswith("xoxp-")
    private, warning = cfg.private and not user_token, None
    try:
        types = "public_channel,private_channel" if private else "public_channel"
        chans = await api.pages(
            "conversations.list", "channels", types=types, exclude_archived="false", limit=PAGE
        )
    except MissingScope:
        if not private:
            raise
        private, warning = False, "Private channels skipped: the token needs groups:read."
        log.warning("slack.private_skipped", team=cfg.team)
        chans = await api.pages(
            "conversations.list", "channels", types="public_channel", exclude_archived="false",
            limit=PAGE,
        )  # fmt: skip
    users = [
        u for u in await api.pages("users.list", "members", limit=PAGE) if is_human(u, cfg.team)
    ]
    live = [u for u in users if not u.get("deleted")]
    full = frozenset(u["id"] for u in live if not is_guest(u))
    guests = {u["id"] for u in live if is_guest(u)}
    world = World(
        cfg.team,
        me.get("team") or cfg.team,
        me.get("url") or "https://slack.com/",
        {c["id"]: c for c in chans},
        full,
        {},
        {u["id"]: u for u in users},
        user_token,
        private,
        warning,
    )
    for cid, ch in sorted(world.roots(cfg.exclude).items()):
        if ch.get("is_private") or guests:
            members = set(
                await api.pages("conversations.members", "members", channel=cid, limit=PAGE)
            )
            if ch.get("is_private"):
                world.inside[cid] = frozenset((full | guests) & members)
            else:
                world.guests[cid] = frozenset(guests & members)
    return world


@dataclass
class Run:
    """One run's reading position; `state` is what the cursor keeps."""

    url: str
    team: str
    chans: dict[str, str]
    """Public channel id to name: the only names a window may show."""
    rewalk: date
    cycle: int
    state: Json
    users: dict[str, str] = field(default_factory=dict[str, str])
    done: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    """(channel, thread ts) rebuilt in this run."""

    @property
    def streams(self) -> dict[str, list[Any]]:
        """Channel id to `[day, cycle]`: days before `day` are stored."""
        return self.state["streams"]

    @property
    def threads(self) -> dict[str, dict[str, float]]:
        """Channel id to `{thread ts: last stored reply}` for threads replied to in the window."""
        return self.state["threads"]

    def trim(self) -> None:
        floor = float(epoch(self.rewalk))
        for cid, found in self.threads.items():
            self.threads[cid] = {t: last for t, last in found.items() if last >= floor}

    def advance(self, cid: str, day: date) -> bool:
        before, self.streams[cid] = self.streams.get(cid), [day.isoformat(), self.cycle]
        return (
            self.streams[cid] != before
        )  # no progress, no stop: what was re-read is rewritten anyway


def group_of(team: str, cid: str) -> str:
    return subject(PROVIDER, team, "channel", cid)


class SlackConnector(Connector):
    kind: ClassVar[str] = "slack"
    name: ClassVar[str] = "Slack"
    category: ClassVar[str] = "chat"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Bot or user token", "secret", placeholder="xoxb-… or xoxp-…"),
        Field("workspace", "Workspace ID", placeholder="T…"),
        Field("private", "Private channels", "select", ("yes", "no"), required=False),
        Field("exclude", "Never read", "list", required=False, placeholder="channel IDs or names"),
        Field("rewalk_days", "Days re-read for edits and replies", required=False, placeholder="7"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport
        self._worlds: dict[object, World] = {}

    def _client(self, source: Source) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {token_of(source)}"},
            timeout=30,
            transport=self.transport or GuardedTransport(source.private_hosts),
        )

    async def _world(self, source: Source, cfg: Config) -> World:
        """The workspace, read once per run (the pass reads it; accounts and content reuse it)."""
        if (world := self._worlds.get(source.id)) is None:
            async with self._client(source) as client:
                world = await read_world(Api(client, source), cfg, token_of(source))
            self._worlds[source.id] = world
        return world

    async def release(self, source: Source) -> None:
        self._worlds.pop(source.id, None)

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            cfg = Config.of(source.config)
            async with self._client(source) as client:
                world = await read_world(Api(client, source), cfg, token_of(source))
        except (ValueError, Upstream) as e:
            return False, str(e)
        note = f"{world.name}: {len(world.roots(cfg.exclude))} channels to read"
        return True, f"{note}. {world.warning}" if world.warning else note

    def warning(self, source: Source) -> str | None:
        world = self._worlds.get(source.id)
        return world.warning if world else None

    # ---- who reads what ----

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit, complete every run: a group per channel read, empty ones included (a group
        missing from the batch loses all its members)."""
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        groups = {
            group_of(cfg.team, cid): tuple(subject(PROVIDER, cfg.team, "user", u) for u in users)
            for cid, users in world.access(cfg.exclude).items()
        }
        yield PermissionUnit(
            "pass", f"{cfg.team}:channels", PASS_MINUTES, groups, time.time_ns() // 1000
        )

    async def accounts(self, source: Source) -> Roster | None:
        """Our people; an email only where Slack confirmed it (it needs `users:read.email`)."""
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        people: list[SourceAccount] = []
        for u in world.people.values():
            prof = obj(u.get("profile"))
            email = prof.get("email") if prof.get("email") and u.get("is_email_confirmed") else None
            name = prof.get("display_name") or u.get("real_name") or u.get("name") or ""
            people.append(SourceAccount(u["id"], name, email, bool(email), not u.get("deleted")))
        return Roster(PROVIDER, cfg.team, tuple(people))

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config.of(source.config)
        world = await self._world(source, cfg)
        roots = {
            cid: (ch, self._facets(ch)) for cid, ch in sorted(world.roots(cfg.exclude).items())
        }
        old = source.state
        had = obj(old.get("streams"))

        def excluded(cid: str) -> bool:
            """Left out by the config: excluded, or private with `private` off (not listed)."""
            if cid not in world.channels:
                return not cfg.private
            name = str(world.channels[cid].get("name") or "")
            return bool({cid.lower(), name.lower()} & cfg.exclude)

        lost = dict(obj(old.get("lost")))
        missing = {c for c in had if c not in roots and not excluded(c)}
        gone = {c for c in had if c not in roots and excluded(c)}
        gone |= lost_for_good(lost, missing, datetime.now(UTC))
        state: Json = {
            "v": 1,
            "cycle": old.get("cycle", 0) + (old.get("complete") is not False),
            "complete": False,
            "team": cfg.team,
            "streams": {k: v for k, v in had.items() if k not in gone},
            "threads": {k: dict(v) for k, v in obj(old.get("threads")).items() if k not in gone},
            "lost": lost,
        }
        for cid in sorted(gone):  # excluded, or lost for good: its windows and cursor go
            yield Replaced(f"conversation:{cid}:", "")
        run = Run(
            world.url,
            cfg.team,
            {
                c["id"]: c.get("name") or c["id"]
                for c in world.channels.values()
                if not c.get("is_private")
            },
            datetime.now(UTC).date() - timedelta(days=cfg.days),
            state["cycle"],
            state,
        )
        run.trim()
        async with self._client(source) as client:
            api = Api(client, source)
            for cid, (ch, facets) in roots.items():
                try:
                    async for rec in self._channel(api, run, cid, ch, facets):
                        yield rec
                except Unreadable as e:
                    log.info("slack.channel_unreadable", channel=cid, reason=str(e))
                    continue
                except MissingScope as e:  # e.g. groups:history: skip it, keep the source going
                    log.warning("slack.channel_skipped", channel=cid, reason=str(e))
                    world.warning = f"Some private channels skipped: {e}."
                    continue
                run.trim()
                yield Checkpoint(state)
        state["complete"] = True
        yield SyncState(state)

    @staticmethod
    def _facets(ch: Json) -> Json:
        shared = (
            ch.get("is_ext_shared")
            or ch.get("is_pending_ext_shared")
            or (ch.get("is_shared") and not ch.get("is_org_shared"))
        )
        return {"channel_id": ch["id"], "shared": "yes" if shared else "no"}

    async def _channel(
        self, api: Api, run: Run, cid: str, ch: Json, facets: Json
    ) -> AsyncGenerator[Record]:
        """One channel from its cursor day (earlier in the re-walk window once per cycle)."""
        cur, today = run.streams.get(cid), datetime.now(UTC).date()
        day = date.fromisoformat(cur[0]) if cur else stamp(str(ch.get("created") or 0)).date()
        if cur and cur[1] != run.cycle:
            day = min(day, run.rewalk)
        lo, probe = day, True
        while True:
            hi = None if probe else lo + timedelta(days=SPAN)
            if hi and hi > today:
                hi = None
            msgs, whole = await self._span(api, cid, lo, hi, probe)
            if not whole:  # busy: read it in spans
                probe = False
                continue
            probe = False
            async for rec in self._days(api, run, cid, ch, facets, msgs, lo, hi):
                yield rec
            if hi is None:
                break
            lo = hi
        async for rec in self._active(api, run, cid, ch, facets):
            yield rec

    @staticmethod
    async def _span(
        api: Api, cid: str, lo: date, hi: date | None, probe: bool
    ) -> tuple[list[Json], bool]:
        """Messages from day lo up to day hi (None: now), oldest first. With `probe`, False as
        soon as they need more than a page."""
        out: dict[str, Json] = {}
        cursor = None
        while True:
            j = await api.call(
                "conversations.history",
                True,
                channel=cid,
                oldest=epoch(lo),
                latest=epoch(hi) if hi else None,
                inclusive="true",
                limit=PAGE,
                cursor=cursor,
            )
            out |= {m["ts"]: m for m in j.get("messages", [])}
            cursor = obj(j.get("response_metadata")).get("next_cursor")
            if not (j.get("has_more") and cursor):
                break
            if probe:
                return [], False
        end = float(epoch(hi)) if hi else None
        return sorted(
            (m for m in out.values() if end is None or float(m["ts"]) < end),
            key=lambda m: float(m["ts"]),
        ), True

    async def _days(
        self,
        api: Api,
        run: Run,
        cid: str,
        ch: Json,
        facets: Json,
        msgs: list[Json],
        lo: date,
        hi: date | None,
    ) -> AsyncGenerator[Record]:
        """Each day of top-level messages: its windows, then the threads whose parents are among
        them, then the days up to it are replaced and the cursor moves on, so a long span is cut
        at a day, not rolled back. An empty read may be spurious: it deletes nothing."""
        top = [m for m in msgs if m.get("thread_ts") in (None, m["ts"])]
        await self._resolve(api, run, top)
        by_day: dict[date, list[Json]] = {}
        for m in top:
            by_day.setdefault(stamp(m["ts"]).date(), []).append(m)
        pos = hi or (stamp(msgs[-1]["ts"]).date() if msgs else lo)
        threads = run.threads.setdefault(cid, {})
        if msgs:
            seen = {m["ts"] for m in msgs}
            a, b = float(epoch(lo)), float(epoch(hi)) if hi else math.inf
            for tts in [t for t in threads if a <= float(t) < b and t not in seen]:
                threads.pop(tts)  # the parent is gone: so are its replies
                yield Replaced(f"conversation:{cid}:t:{tts}:", "")
        start = lo
        for d in sorted(by_day):
            records = self._windows(run, cid, ch, None, d.isoformat(), by_day[d], facets)
            for rec in records:
                yield rec
            for p in by_day[d]:
                tts = p["ts"]
                if not p.get("reply_count") and tts not in threads:  # no replies, none stored
                    continue
                run.done.add((cid, tts))
                if p.get("subtype") == "tombstone":
                    threads.pop(tts, None)
                    yield Replaced(f"conversation:{cid}:t:{tts}:", "")
                elif p.get("subtype") in KEEP:
                    async for rec in self._thread(api, run, cid, ch, facets, p):
                        yield rec
            end = d + timedelta(days=1)
            yield Replaced(
                f"conversation:{cid}:d:",
                start.isoformat(),
                end.isoformat(),
                {r.external_id for r in records},
            )
            start = end
            if run.advance(cid, min(end, pos)):
                yield Checkpoint(run.state)
        if by_day:
            yield Replaced(f"conversation:{cid}:d:", start.isoformat(), hi and hi.isoformat())
        if run.advance(cid, pos):
            yield Checkpoint(run.state)

    async def _active(
        self, api: Api, run: Run, cid: str, ch: Json, facets: Json
    ) -> AsyncGenerator[Record]:
        """Threads with a recent reply whose parent this run did not read: new replies count."""
        for tts, last in sorted(run.threads.get(cid, {}).items()):
            if (cid, tts) in run.done:
                continue
            try:
                j = await api.call(
                    "conversations.replies",
                    True,
                    channel=cid,
                    ts=tts,
                    oldest=f"{last:.6f}",
                    limit=1,
                )
            except Unreadable as e:
                if "thread_not_found" not in str(e):
                    raise
                j = None
            parent = next((m for m in (j or {}).get("messages", []) if m["ts"] == tts), None)
            if (
                j is None or parent and parent.get("subtype") == "tombstone"
            ):  # only these say it is gone
                run.threads[cid].pop(tts, None)
                yield Replaced(f"conversation:{cid}:t:{tts}:", "")
            elif parent and parent.get("subtype") in KEEP:
                async for rec in self._thread(api, run, cid, ch, facets, parent):
                    yield rec

    async def _thread(
        self, api: Api, run: Run, cid: str, ch: Json, facets: Json, parent: Json
    ) -> AsyncGenerator[Record]:
        """A thread rebuilt whole: replies by day of reply. A deleted parent leaves no windows."""
        tts, replies = parent["ts"], []
        try:
            found = await api.pages(
                "conversations.replies", "messages", True, channel=cid, ts=tts, limit=PAGE
            )
            replies = [m for m in found if m["ts"] != tts]
        except Unreadable as e:
            if "thread_not_found" not in str(e):
                raise
        await self._resolve(api, run, [parent, *replies])
        by_day: dict[str, list[Json]] = {}
        for m in sorted(replies, key=lambda m: float(m["ts"])):
            by_day.setdefault(stamp(m["ts"]).date().isoformat(), []).append(m)
        keep: set[str] = set()
        for d, ms in by_day.items():
            for rec in self._windows(run, cid, ch, parent, d, ms, facets):
                keep.add(rec.external_id)
                yield rec
        yield Replaced(
            f"conversation:{cid}:t:{tts}:", "", keep=keep
        )  # no replies left means none stored
        run.threads.setdefault(cid, {}).pop(tts, None)
        if replies and (last := max(float(m["ts"]) for m in replies)) >= float(epoch(run.rewalk)):
            run.threads[cid][tts] = last

    @staticmethod
    async def _resolve(api: Api, run: Run, msgs: list[Json]) -> None:
        """Display names of the authors and people mentioned, one lookup each per run."""
        for m in msgs:
            prof = obj(m.get("user_profile"))
            if (
                m.get("user")
                and m["user"] not in run.users
                and (n := prof.get("display_name") or prof.get("real_name"))
            ):
                run.users[m["user"]] = n
        want = {
            u
            for m in msgs
            for u in [m.get("user"), *re.findall(r"<@([UW]\w+)", m.get("text") or "")]
            if u
        }
        for u in sorted(want - set(run.users)):
            try:
                p = (await api.call("users.info", True, user=u))["user"]
                run.users[u] = (
                    obj(p.get("profile")).get("display_name")
                    or p.get("real_name")
                    or p.get("name")
                    or u
                )
            except (Upstream, Unreadable, KeyError):
                run.users[u] = u

    @staticmethod
    def _text(run: Run, m: Json) -> str:
        """A channel mention shows the name the public list gives, never the label in the message
        (it may name a private channel)."""
        t = m.get("text") or ""
        t = re.sub(r"<@([UW]\w+)(?:\|[^>]*)?>", lambda x: "@" + run.users.get(x[1], x[1]), t)
        t = re.sub(r"<#(\w+)(?:\|[^>]*)?>", lambda x: "#" + (run.chans.get(x[1]) or "channel"), t)
        t = re.sub(r"<!subteam\^\w+\|([^>]*)>", r"\1", t)
        t = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", t)
        t = re.sub(r"<[^|>]+\|([^>]+)>", r"\1", t)
        t = html.unescape(re.sub(r"<([^>]+)>", r"\1", t))
        t = re.sub(r"\s*\n\s*", " / ", t.strip())
        files = " ".join(
            f"[file: {f.get('name') or f.get('title') or 'file'}]" for f in rows(m.get("files"))
        )
        return " ".join(x for x in (t, files) if x)

    def _windows(
        self,
        run: Run,
        cid: str,
        ch: Json,
        parent: Json | None,
        day: str,
        msgs: list[Json],
        facets: Json,
    ) -> list[ItemRecord]:
        """One day as `HH:MM Author: text` lines, split past `WINDOW` characters."""
        lines: list[str] = []
        ids: list[str] = []
        authors: list[str] = []
        files: list[list[Attachment]] = []
        for m in msgs:
            if m.get("bot_id") or m.get("subtype") not in KEEP or not (text := self._text(run, m)):
                continue
            uid = str(m.get("user") or "")
            who = run.users.get(uid) or uid or m.get("username") or "unknown"
            n = m.get("reply_count")
            thread_note = (
                f" [thread: {n} {'reply' if n == 1 else 'replies'}]" if n and not parent else ""
            )
            lines.append(f"{stamp(m['ts']):%H:%M} {who}: {text}{thread_note}")
            ids.append(m["ts"])
            authors.append(who)
            files.append(
                [
                    Attachment(
                        f.get("name") or f.get("title") or "file",
                        f.get("mimetype") or "",
                        None,
                        int(f.get("size") or 0),
                    )
                    for f in rows(m.get("files"))
                ]
            )
        name = f"#{ch.get('name')}" + (
            f" › {(self._text(run, parent) or 'thread')[:50]}" if parent else ""
        )
        tts = parent and parent["ts"]
        access = Acl((AclRef(group_of(run.team, cid), group=True),))
        parts = split(lines)
        out: list[ItemRecord] = []
        for k, part in enumerate(parts, 1):
            who = list(dict.fromkeys(authors[i] for i in part))
            first = ids[part[0]]
            out.append(
                ItemRecord(
                    external_id=f"conversation:{cid}:"
                    + (f"t:{tts}:" if parent else "d:")
                    + day
                    + (f":{k}" if k > 1 else ""),
                    kind="conversation",
                    title=f"{name} · {day}" + (f" ({k}/{len(parts)})" if len(parts) > 1 else ""),
                    body="\n".join(lines[i] for i in part),
                    occurred_at=stamp(first),
                    container=ch.get("name"),
                    author=authors_of(who),
                    url=f"{run.url.rstrip('/')}/archives/{cid}/p{first.replace('.', '')}"
                    + (f"?thread_ts={tts}&cid={cid}" if parent else ""),
                    payload={
                        "channel_id": cid,
                        **({"thread_ts": tts} if parent else {}),
                        "first": first,
                        "last": ids[part[-1]],
                        "authors": who,
                        "lines": [ids[i] for i in part],
                    },
                    facets={**facets, **({"thread_id": tts} if parent else {})},
                    acl=access,
                    files=[f for i in part for f in files[i]],
                )
            )
        return out

    async def fetch_file(
        self, source: Source, payload: Json, message: str, name: str, index: int = 0
    ) -> tuple[bytes, str] | None:
        """The message read now (a reply in its thread) for its files, then the one of that name,
        with the source's token."""
        cid, tts = payload.get("channel_id"), payload.get("thread_ts")
        at = {"latest": message, "oldest": message, "inclusive": "true"}
        async with self._client(source) as client:
            api = Api(client, source)
            try:
                j = await (
                    api.call("conversations.replies", True, channel=cid, ts=tts, limit=2, **at)
                    if tts
                    else api.call("conversations.history", True, channel=cid, limit=1, **at)
                )
            except Unreadable:
                return None
        m = next((m for m in rows(j.get("messages")) if m.get("ts") == message), Json())
        named = [
            f for f in rows(m.get("files")) if (f.get("name") or f.get("title") or "file") == name
        ]
        if index >= len(named) or int(named[index].get("size") or 0) > BLOB_CAP:
            return None
        found = named[index]
        data = await download(
            self.transport or GuardedTransport(source.private_hosts),
            found.get("url_private_download") or found.get("url_private"),
            FILES,
            BLOB_CAP,
            {"Authorization": f"Bearer {token_of(source)}"},
        )
        return None if data is None else (data, found.get("mimetype") or "application/octet-stream")
