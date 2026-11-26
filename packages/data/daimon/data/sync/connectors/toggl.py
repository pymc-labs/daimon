"""Toggl Track: a workspace's time entries, projects and clients, each readable by the people
Toggl shows it to, or less (see `toggl_api.py` for the rule). A source of this kind is stored
(synced) or, created with mode `federated`, read live as the person asking.

Stored: every run reads the projects and clients (a missing one is removed) and the entries
changed since the last run, newest edit first. A full pass reads every entry in ranges of
`RANGE_DAYS` calendar days (each fetched a day wider at both ends, since a report's days are
its authors' own) and removes the entries of a range it no longer lists: a daily pass over
the last year, and a weekly one back to `config.history_from`. A pass continues over runs.
Rates, amounts, costs and currency are never read into a record. A live search, or opening a
stored entry, adds them from Toggl then (members cached a minute) when the linked account is an
admin of the workspace (as Toggl lists it then), and only when everyone the answer is for is one.

Credential `{"token": "..."}`: a workspace admin's API token. Configuration: `workspace` (the
numeric id), `history_from` (YYYY-MM-DD).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar, Final

import httpx
from daimon.data.identity import SourceAccount
from daimon.data.sync.connector import (
    Checkpoint,
    Connector,
    Descriptor,
    Field,
    ItemRecord,
    Json,
    LiveHit,
    PermissionUnit,
    Record,
    Replaced,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.connectors.toggl_api import (
    PROVIDER,
    UNIT_MINUTES,
    Api,
    Config,
    client_of,
    entry_acl,
    ident,
    is_active,
    local_day,
    managers_group,
    metadata_acl,
    now,
    roles_group,
    sees_all,
    seq,
    stamp,
    user_ref,
)
from daimon.data.sync.http import Upstream
from daimon.data.sync.request_cache import once

PAGE_ROWS: Final = 5000
LISTING_DAYS: Final = 366
"""The most days one report search covers."""
MARGIN: Final = timedelta(days=1)
RANGE_DAYS: Final = 180
"""Days of one full-pass range; with the margin it stays inside `LISTING_DAYS`."""
RECENT: Final = timedelta(hours=24)
DEEP: Final = timedelta(hours=168)
OVERLAP: Final = timedelta(minutes=10)
"""A window reads this far before the last read began."""
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
LIVE_DAYS: Final = 90
LIVE_PAGES: Final = 3
"""Report pages one live search reads at most."""
USERS_SECONDS: Final = 60.0
"""How long a source's member list answers the admin check of stored entries' rates."""
_users_seen: dict[uuid.UUID, tuple[float, dict[str, Json]]] = {}
WORD: Final = re.compile(r"\w+")


def at(value: object) -> datetime:
    return stamp(value) or EPOCH


def project_record(workspace: str, p: Json, clients: dict[str, str]) -> ItemRecord:
    private = bool(p.get("is_private", True))
    active = bool(p.get("active", True))
    client = clients.get(ident(p.get("client_id", p.get("cid"))) or "")
    return ItemRecord(
        f"project:{p['id']}",
        "project",
        str(p["name"]),
        f"{p['name']}: {'private' if private else 'public'} project"
        + ("" if active else ", archived"),
        at(p.get("at")),
        container=str(p["name"]),
        container_kind="project",
        client=client,
        payload={"active": active, "is_private": private, "billable": bool(p.get("billable"))},
        acl=metadata_acl(workspace),
    )


def client_record(workspace: str, c: Json) -> ItemRecord:
    archived = bool(c.get("archived"))
    return ItemRecord(
        f"client:{c['id']}",
        "client",
        str(c["name"]),
        f"{c['name']}: client" + (", archived" if archived else ""),
        at(c.get("at")),
        client=str(c["name"]),
        payload={"archived": archived},
        acl=metadata_acl(workspace),
    )


def entry_text(line: Json, e: Json, project: str | None, client: str | None) -> tuple[str, str]:
    """(title, text) of one entry: description, project, hours, day and author. No rate or cost."""
    start, stop = stamp(e["start"]), stamp(e.get("stop"))
    counted = e.get("seconds")
    seconds = (
        counted
        if isinstance(counted, int) and counted >= 0
        else int((stop - start).total_seconds())
        if stop and start
        else 0
    )
    desc, who = str(line.get("description") or ""), str(line.get("username") or "")
    hours = "running" if stop is None else f"{seconds / 3600:.2f} h"
    where = (project or "No project") + (f" ({client})" if client else "")
    day = local_day(e["start"]).isoformat()
    text = (
        f"{desc or 'No description'}. {where}, {hours} on {day}"
        + (f", by {who}" if who else "")
        + "."
    )
    return desc or "Time entry", text


def entry_record(
    workspace: str, line: Json, e: Json, projects: dict[str, Json], clients: dict[str, str]
) -> ItemRecord:
    """One entry of a report row. Its id carries its day, so that a range pass removes by day."""
    start, stop = at(e["start"]), stamp(e.get("stop"))
    pid = ident(line.get("project_id"))
    p = projects.get(pid or "", {})
    project = str(p["name"]) if p else None
    client = clients.get(ident(p.get("client_id", p.get("cid"))) or "") if p else None
    title, text = entry_text(line, e, project, client)
    return ItemRecord(
        f"time_entry:{local_day(e['start']).isoformat()}:{e['id']}",
        "time_entry",
        title,
        text,
        start,
        container=project,
        container_kind="project",
        author=str(line.get("username") or "") or None,
        client=client,
        payload={
            "user_id": str(line["user_id"]),
            "project_id": pid,
            "billable": bool(line.get("billable")),
            "started_at": start.isoformat(),
            "stopped_at": stop.isoformat() if stop else None,
            "running": stop is None,
        },
        acl=entry_acl(workspace, line["user_id"], pid),
    )


def rates_text(line: Json, e: Json) -> str:
    """An entry's hourly rate, billable amount and labor cost, for a Toggl admin; never stored."""
    rate, cur = line.get("hourly_rate_in_cents"), str(line.get("currency") or "")
    seconds = e.get("seconds")
    seconds = seconds if isinstance(seconds, int) else 0
    parts: list[str] = []
    if isinstance(rate, int | float):
        billed = e.get(
            "billable_amount_in_cents", rate * seconds / 3600 if line.get("billable") else 0
        )
        parts += [f"rate {rate / 100:.2f} {cur}/h", f"billable {float(billed) / 100:.2f} {cur}"]
    cost = e.get("labor_cost_in_cents", line.get("labor_cost_in_cents"))
    if isinstance(cost, int | float):
        parts.append(f"labor cost {cost / 100:.2f} {cur}")
    return f" Billing: {', '.join(parts)}." if parts else ""


def terms(query: str) -> tuple[list[str], list[str]]:
    """(words every hit must hold, words none may hold: `-word`)."""
    want: list[str] = []
    without: list[str] = []
    for tok in query.lower().split():
        words = [w for w in WORD.findall(tok) if w != "or"]
        (without if tok.startswith("-") else want).extend(words)
    return want, without


def holds(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}", text) is not None


class TogglConnector(Connector):
    kind: ClassVar[str] = "toggl"
    name: ClassVar[str] = "Toggl Track"
    category: ClassVar[str] = "time"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    live: ClassVar[bool] = True
    live_as_reader: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "API token", "secret"),
        Field("workspace", "Workspace ID", placeholder="1234567"),
        Field("history_from", "First day to read", required=False, placeholder="2010-01-01"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    def workspace_of(self, source: Source) -> str:
        return Config(source.config).workspace

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            cfg = Config(source.config)
            async with client_of(source, self.transport) as client:
                await Api(client, source, cfg.workspace).json(f"/api/v9/workspaces/{cfg.workspace}")
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return True, "ok"

    # ---- who reads ----

    async def accounts(self, source: Source) -> Roster | None:
        """The workspace's members. Toggl verifies no address: an admin links each by hand."""
        cfg = Config(source.config)
        async with client_of(source, self.transport) as client:
            users = await Api(client, source, cfg.workspace).users()
        return Roster(
            PROVIDER,
            cfg.workspace,
            tuple(
                SourceAccount(
                    str(u["uid"]), str(u.get("name") or ""), u.get("email"), False, is_active(u)
                )
                for u in users
            ),
        )

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit per active member, one for the roles that read everything and one for the
        project managers."""
        cfg = Config(source.config)
        ws = cfg.workspace
        async with client_of(source, self.transport) as client:
            api = Api(client, source, ws)
            users, memberships = await api.users(), await api.managers()
        active = {str(u["uid"]): u for u in users if is_active(u)}
        n = seq()
        for uid in active:
            mine = user_ref(ws, uid)
            yield PermissionUnit("person", mine, UNIT_MINUTES, {}, n)
        everyone = tuple(user_ref(ws, uid) for uid, u in active.items() if sees_all(u))
        yield PermissionUnit("pass", f"{ws}:roles", UNIT_MINUTES, {roles_group(ws): everyone}, n)
        managers: dict[str, list[str]] = {}  # a project missing from the batch loses its managers
        for m in memberships:
            if m.get("manager") and str(m["user_id"]) in active:
                managers.setdefault(managers_group(ws, m["project_id"]), []).append(
                    user_ref(ws, m["user_id"])
                )
        yield PermissionUnit(
            "pass", f"{ws}:managers", UNIT_MINUTES, {g: tuple(m) for g, m in managers.items()}, n
        )

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config(source.config)
        began = now()
        old = source.state if source.state.get("sig") == cfg.signature else {}
        state: Json = {"v": 1, "sig": cfg.signature}
        state |= {k: old.get(k) for k in ("since", "recent_at", "deep_at", "pass")}
        async with client_of(source, self.transport) as client:
            api = Api(client, source, cfg.workspace)
            listed = await self._clients(api)
            clients = {str(c["id"]): str(c["name"]) for c in listed}
            projects = {
                str(p["id"]): p
                for p in await api.pages(f"{api.base}/projects", active="both")
                if not p.get("server_deleted_at")
            }
            ws = cfg.workspace
            for kind, made in (
                ("client", [client_record(ws, c) for c in listed]),
                ("project", [project_record(ws, p, clients) for p in projects.values()]),
            ):
                for rec in made:
                    yield rec
                yield Replaced(f"{kind}:", "", None, {r.external_id for r in made})
            yield Checkpoint(dict(state))
            run: Json | None = obj(state["pass"]) or self._plan(state, cfg, began)
            if run:
                async for rec in self._pass(api, ws, run, state, projects, clients):
                    yield rec
            else:
                async for rec in self._window(api, ws, state, began, projects, clients):
                    yield rec
        yield SyncState(state)

    @staticmethod
    async def _clients(api: Api) -> list[Json]:
        body = await api.json(f"{api.base}/clients", status="both")
        return [c for c in rows(body) if not c.get("server_deleted_at")]

    @staticmethod
    def _plan(state: Json, cfg: Config, began: datetime) -> Json | None:
        """The pass to begin (a weekly one from `history_from`, a daily one over the last
        year), or None: a window will do."""
        deep, recent = state.get("deep_at"), state.get("recent_at")
        if not deep or datetime.fromisoformat(deep) <= began - DEEP:
            first = cfg.history_from
        elif not recent or datetime.fromisoformat(recent) <= began - RECENT:
            first = (began - timedelta(days=LISTING_DAYS - 1)).date()
        else:
            return None
        return {
            "began": began.isoformat(),
            "day": first.isoformat(),
            "last": began.date().isoformat(),
            "deep": first == cfg.history_from,
        }

    @staticmethod
    async def _pass(
        api: Api,
        ws: str,
        run: Json,
        state: Json,
        projects: dict[str, Json],
        clients: dict[str, str],
    ) -> AsyncGenerator[Record]:
        """The entries of each range, then the removal of the range's entries not seen. The
        state moves on only between ranges: a cut run starts its range again."""
        day, last = date.fromisoformat(run["day"]), date.fromisoformat(run["last"])
        while day <= last:
            end = min(day + timedelta(days=RANGE_DAYS - 1), last)
            kept: set[str] = set()
            row: int | None = None
            while True:
                lines, row = await api.report(
                    {
                        "grouped": False,
                        "page_size": PAGE_ROWS,
                        "start_date": (day - MARGIN).isoformat(),
                        "end_date": (end + MARGIN).isoformat(),
                    },
                    row,
                )
                for line in lines:
                    for e in rows(line.get("time_entries")):
                        if day <= local_day(e["start"]) <= end:
                            rec = entry_record(ws, line, e, projects, clients)
                            kept.add(rec.external_id)
                            yield rec
                if row is None:
                    break
                yield Checkpoint({**state, "pass": {**run, "day": day.isoformat()}})
            yield Replaced(
                "time_entry:", day.isoformat(), (end + timedelta(days=1)).isoformat(), kept
            )
            day = end + timedelta(days=1)
            yield Checkpoint({**state, "pass": {**run, "day": day.isoformat()}})
        state["pass"] = None
        state["since"] = run["began"]
        state["recent_at"] = run["began"]
        if run["deep"]:
            state["deep_at"] = run["began"]

    @staticmethod
    async def _window(
        api: Api,
        ws: str,
        state: Json,
        began: datetime,
        projects: dict[str, Json],
        clients: dict[str, str],
    ) -> AsyncGenerator[Record]:
        """Entries changed since the last read, newest edit first, until one older than it."""
        since = datetime.fromisoformat(state["since"]) - OVERLAP
        body = {
            "grouped": False,
            "page_size": PAGE_ROWS,
            "start_date": (began - timedelta(days=LISTING_DAYS - 1)).date().isoformat(),
            "end_date": (began + MARGIN).date().isoformat(),
            "order_by": "last_update",
            "order_dir": "DESC",
        }
        row: int | None = None
        while True:
            lines, row = await api.report(body, row)
            older = False
            for line in lines:
                for e in rows(line.get("time_entries")):
                    if at(e.get("at")) < since:
                        older = True
                    else:
                        yield entry_record(ws, line, e, projects, clients)
            if older or row is None:
                break
            yield Checkpoint(dict(state))
        state["since"] = began.isoformat()

    # ---- live ----

    async def _live_read(
        self, source: Source, cfg: Config
    ) -> tuple[dict[str, Json], list[Json], dict[str, str], list[Json]]:
        """What every reader's live search needs of Toggl: users, managers, project names and
        the recent report rows. Read once per request."""
        async with client_of(source, self.transport) as client:
            api = Api(client, source, cfg.workspace, live=True)
            users = {str(u["uid"]): u for u in await api.users()}
            managers = list(await api.managers())
            names = {
                str(p["id"]): str(p["name"])
                for p in await api.pages(f"{api.base}/projects", active="both")
            }
            today = now().date()
            body = {
                "grouped": False,
                "page_size": PAGE_ROWS,
                "start_date": (today - timedelta(days=LIVE_DAYS)).isoformat(),
                "end_date": (today + MARGIN).isoformat(),
                "order_by": "date",
                "order_dir": "DESC",
            }
            lines: list[Json] = []
            row: int | None = None
            for _ in range(LIVE_PAGES):
                page, row = await api.report(body, row)
                lines += page
                if row is None:
                    break
        return users, managers, names, lines

    async def live_search_as(
        self, source: Source, query: str, limit: int, subjects: Sequence[str]
    ) -> list[LiveHit]:
        """Entries of the last `LIVE_DAYS` days that match every word of `query` and that one
        person may read now: their own, those of projects they manage, or all for a holder of
        an "all" role. Read from Toggl at question time; the answer is never stored."""
        cfg = Config(source.config)
        users, managers, names, lines = await once(
            ("toggl-live", source.id), lambda: self._live_read(source, cfg)
        )
        mine = {i for i in subjects if i in users and is_active(users[i])}
        if not mine:
            return []
        everything = any(sees_all(users[i]) for i in mine)
        admin = any(users[i].get("admin") is True for i in mine)
        managed: set[str] = (
            set()
            if everything
            else {
                str(m["project_id"])
                for m in managers
                if m.get("manager") and str(m["user_id"]) in mine
            }
        )
        want, without = terms(query)
        hits: list[LiveHit] = []
        for line in lines:
            pid = ident(line.get("project_id"))
            if not (everything or str(line["user_id"]) in mine or (pid and pid in managed)):
                continue
            for e in rows(line.get("time_entries")):
                project, who = names.get(pid or ""), str(line.get("username") or "")
                title, text = entry_text(line, e, project, None)
                hay = f"{title} {project or ''} {who}".lower()
                if all(holds(hay, w) for w in want) and not any(holds(hay, w) for w in without):
                    hits.append(
                        LiveHit(
                            f"{e['id']}@{local_day(e['start'])}",
                            title,
                            text,
                            "time_entry",
                            None,
                            who or None,
                            at(e["start"]).astimezone(UTC),
                            rates_text(line, e) if admin else "",
                        )
                    )
        hits.sort(key=lambda h: h.occurred_at or EPOCH, reverse=True)
        return hits[:limit]

    async def _users(self, source: Source, cfg: Config) -> dict[str, Json]:
        """The workspace's members by id, read again after `USERS_SECONDS`."""
        seen = _users_seen.get(source.id)
        if seen and time.monotonic() - seen[0] < USERS_SECONDS:
            return seen[1]
        async with client_of(source, self.transport) as client:
            api = Api(client, source, cfg.workspace, live=True)
            users = {str(u["uid"]): u for u in await api.users()}
        _users_seen[source.id] = (time.monotonic(), users)
        return users

    async def live_extra(
        self, source: Source, external_ids: Sequence[str], subjects: Sequence[tuple[str, ...]]
    ) -> dict[str, str]:
        """The rates, amounts and costs of these stored entries, read from Toggl now, when
        every reader's linked account is an active admin of the workspace; never stored."""
        entries: dict[int, tuple[str, date]] = {}
        for ext in external_ids:
            kind, _, rest = ext.partition(":")
            day, _, eid = rest.partition(":")
            if kind == "time_entry" and eid.isdigit():
                entries[int(eid)] = (ext, date.fromisoformat(day))
        if not entries or not subjects:
            return {}
        cfg = Config(source.config)
        users = await self._users(source, cfg)
        if not all(
            any(i in users and is_active(users[i]) and users[i].get("admin") is True for i in s)
            for s in subjects
        ):
            return {}
        async with client_of(source, self.transport) as client:
            api = Api(client, source, cfg.workspace, live=True)
            days = [d for _, d in entries.values()]
            lines, _ = await api.report(
                {
                    "grouped": False,
                    "page_size": PAGE_ROWS,
                    "time_entry_ids": sorted(entries),
                    "start_date": (min(days) - MARGIN).isoformat(),
                    "end_date": (max(days) + MARGIN).isoformat(),
                },
                None,
            )
        return {
            entries[e["id"]][0]: text
            for line in lines
            for e in rows(line.get("time_entries"))
            if e.get("id") in entries and (text := rates_text(line, e))
        }
