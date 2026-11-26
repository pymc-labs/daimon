"""Google Drive: files of shared drives, readable by the people Drive itself lets read them,
never more. A person's own My Drive is never stored: `my_drive_search` reads it as them, at
their request (`daimon.data.my_drive`), while `config.personal_drive` is on (the default).

Each file's readers come from its own permission list, read as the admin by domain admin
access. A permission grants a person of the domain by primary email, a group (its members
come from the Directory API, read by this source or by another source of its `config.group`,
the same name on each source of one Google connection), the whole domain
when the share is discoverable, and nobody else: link-only shares, other domains, external
people, deleted and expiring permissions grant nothing here. A file nobody inside may read is
not stored. The access evidence of a file lapses `relist_hours` + 12 h after it was read, so a
source that stops syncing stops granting.

Content: Docs, Slides and Sheets (first tab) are exported as text; text and Office files are
downloaded for the engine to extract (PDFs and other binaries keep metadata only); anything over
`max_mb` is listed without content. A run follows each scope's change feed and lists everything
again every `relist_hours` (a folder's new shares reach the files below it then) or when a feed
cursor has expired or the scopes changed.

Scopes of the service account's delegation: `drive.readonly`,
`admin.directory.user.readonly`, `admin.directory.group.readonly`. See `docs/data-connectors.md`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

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
    LiveHit,
    PermissionUnit,
    Record,
    Replaced,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import names, obj, rows, switch
from daimon.data.sync.connectors.google import (
    ApiError,
    DelegationRefused,
    Directory,
    Google,
    connect,
    person_email,
    read_directory,
)
from daimon.data.sync.http import Upstream
from daimon.data.sync.office import DOCX, PPTX, TEXT_CAP, XLSX

log = structlog.get_logger()

PROVIDER: Final = "google"
FOLDER: Final = "application/vnd.google-apps.folder"
EXPORT: Final = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
"""Google's own formats and the text each is exported as."""
OFFICE: Final = {DOCX, XLSX, PPTX, "application/json", "application/xml"}
PDF: Final = "application/pdf"
FILE_FIELDS: Final = (
    "id,name,mimeType,parents,driveId,modifiedTime,size,trashed,ownedByMe,webViewLink,"
    "owners(emailAddress)"
)
CHANGE_FIELDS: Final = (
    f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"
)
PERMISSION_FIELDS: Final = (
    "permissions(id,type,role,emailAddress,domain,allowFileDiscovery,deleted,expirationTime),"
    "nextPageToken"
)
PASS_MINUTES: Final = 60
BATCH: Final = 25
"""Files read at once, and between two checkpoints."""
AT_ONCE: Final = 6
GONE: Final = (403, 404)
"""A file the identity may not open any more: it reads as absent."""
END: Final = "\x01"
"""Sorts after every character of a file id: a `Replaced` that ends here removes one record."""
MY_DRIVE_TEXT: Final = 200_000
"""Bytes of one file's text a personal Drive search returns at most (larger: none)."""


class Expired(Exception):
    """A change feed cursor Google no longer honours: everything is listed again."""


@dataclass(frozen=True)
class Config:
    domain: str
    admin: str
    personal_drive: bool
    drives: tuple[str, ...]
    include: frozenset[str]
    exclude: frozenset[str]
    cap: int
    relist: timedelta

    @classmethod
    def of(cls, config: Json) -> Config:
        domain = str(config.get("domain") or "").strip().lower()
        admin = str(config.get("admin") or "").strip().lower()
        if not domain or not admin.endswith("@" + domain):
            raise ValueError(
                "config.domain and config.admin (an admin of that domain) are required"
            )
        return cls(
            domain,
            admin,
            switch(config, "personal_drive"),
            tuple(names(config, "drives")),
            frozenset(names(config, "include_folders")),
            frozenset(names(config, "exclude_folders")),
            min(_number(config, "max_mb", 20), BLOB_CAP // 1_000_000) * 1_000_000,
            timedelta(hours=_number(config, "relist_hours", 24)),
        )

    @property
    def lease(self) -> timedelta:
        return self.relist + timedelta(hours=12)


def _number(config: Json, key: str, default: int) -> int:
    raw = str(config.get(key) or "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit() and int(raw) >= 1):
        raise ValueError(f"config.{key} must be a whole number, 1 or more")
    return int(raw)


# ---- who reads a file ------------------------------------------------------------------------


def user_subject(domain: str, user_id: str) -> str:
    return subject(PROVIDER, domain, "user", user_id)


def group_subject(domain: str, email: str) -> str:
    return subject(PROVIDER, domain, "group", email.lower())


def members_subject(domain: str) -> str:
    """Every active person of the domain."""
    return subject(PROVIDER, domain, "domain", "members")


def ref_of(d: Directory, p: Json) -> AclRef | None:
    """The reader one permission grants, or None. Drive names a Workspace account by its current
    primary address, so an alias is another account (a personal one on that address) and grants
    nobody. A share to `anyone` or to the domain grants the domain's people only when it is
    discoverable; a link-only share is not a list of readers."""
    if p.get("deleted") or p.get("expirationTime"):
        return None
    kind, email = p.get("type"), str(p.get("emailAddress") or "").lower()
    if kind == "user" and (uid := d.active.get(email)):
        return AclRef(user_subject(d.domain, uid))
    if kind == "group" and email:
        return AclRef(group_subject(d.domain, email), group=True)
    discoverable = bool(p.get("allowFileDiscovery"))
    if kind == "domain" and str(p.get("domain") or "").lower() == d.domain and discoverable:
        return AclRef(members_subject(d.domain), group=True)
    if kind == "anyone" and discoverable:
        return AclRef(members_subject(d.domain), group=True)
    return None


def acl_of(d: Directory, permissions: list[Json], until: datetime) -> Acl | None:
    """None when no permission grants anyone here: the file is not stored."""
    refs = {r.ref: r for p in permissions if (r := ref_of(d, p))}
    return Acl(tuple(refs[k] for k in sorted(refs)), until=until) if refs else None


# ---- one run ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """One place files are read from, with the person it is read as: a shared drive (as the
    admin) or one person's My Drive (as that person)."""

    id: str
    who: str
    drive: str | None = None
    name: str | None = None

    def holds(self, m: Json) -> bool:
        """Whether a changed file belongs to this key."""
        if self.drive:
            return m.get("driveId") == self.drive
        return not m.get("driveId") and bool(m.get("ownedByMe"))


def indexable(m: Json) -> bool:
    mime = str(m.get("mimeType") or "")
    return not m.get("trashed") and (
        mime in EXPORT or mime == PDF or mime.startswith("text/") or mime in OFFICE
    )


def readable(mime: str) -> bool:
    """Files the engine can take text from."""
    return mime.startswith("text/") or mime in OFFICE


@dataclass
class Run:
    """One run's reads: the configuration, the directory, Google, and the folder ancestry."""

    cfg: Config
    google: Google
    directory: Directory
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    parents: dict[str, list[str] | None] = field(default_factory=dict[str, list[str] | None])
    """Folder or file id to its parents; None: not readable, so the chain is unknown."""
    reading: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(AT_ONCE))

    def until(self) -> datetime:
        return self.started + self.cfg.lease

    async def keys(self) -> list[Key]:
        cfg, g = self.cfg, self.google
        found: list[Key] = []
        if cfg.drives:
            drives = [
                await g.get(
                    cfg.admin,
                    "drive",
                    f"/drive/v3/drives/{d}",
                    {"useDomainAdminAccess": "true", "fields": "id,name"},
                )
                for d in cfg.drives
            ]
        else:
            drives = [
                d
                async for d in g.pages(
                    cfg.admin,
                    "drive",
                    "/drive/v3/drives",
                    "drives",
                    {
                        "useDomainAdminAccess": "true",
                        "pageSize": 100,
                        "fields": "drives(id,name),nextPageToken",
                    },
                )
            ]
        found += [Key(f"drive:{d['id']}", cfg.admin, d["id"], d.get("name")) for d in drives]
        return found

    def listing(self, key: Key) -> AsyncIterator[Json]:
        params: dict[str, object] = {
            "pageSize": 500,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
        }
        if key.drive:
            params |= {"corpora": "drive", "driveId": key.drive, "q": "trashed = false"}
        else:
            params |= {"corpora": "user", "q": "'me' in owners and trashed = false"}
        return self.google.pages(key.who, "drive", "/drive/v3/files", "files", params)

    def feed_params(self, key: Key) -> dict[str, object]:
        out: dict[str, object] = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        return out | ({"driveId": key.drive} if key.drive else {})

    async def start_token(self, key: Key) -> str:
        body = await self.google.get(
            key.who, "drive", "/drive/v3/changes/startPageToken", self.feed_params(key)
        )
        return str(body["startPageToken"])

    async def allowed(self, key: Key, m: Json) -> bool:
        """Whether the include and exclude folders let this file be read. A chain that cannot be
        followed to its root fails closed."""
        cfg = self.cfg
        if not (cfg.include or cfg.exclude):
            return True
        seen: set[str] = set()
        todo = [str(m["id"])]
        inside = not cfg.include
        while todo:
            fid = todo.pop()
            if fid in seen:
                continue
            seen.add(fid)
            if fid in cfg.exclude:
                return False
            inside = inside or fid in cfg.include
            if (up := await self.parents_of(key, fid, m)) is None:
                return False
            todo += up
        return inside

    async def parents_of(self, key: Key, fid: str, m: Json) -> list[str] | None:
        if fid == m["id"]:
            return list(m.get("parents") or [])
        if fid == key.drive:
            return []
        if fid not in self.parents:
            try:
                got = await self.google.get(
                    key.who,
                    "drive",
                    f"/drive/v3/files/{fid}",
                    {"supportsAllDrives": "true", "fields": "id,parents"},
                )
                self.parents[fid] = list(got.get("parents") or [])
            except ApiError as e:
                if e.status not in GONE:
                    raise
                self.parents[fid] = None
        return self.parents[fid]

    async def record(self, key: Key, m: Json) -> ItemRecord | None:
        """The file as a record, or None when nobody inside may read it."""
        async with self.reading:
            fid = str(m["id"])
            params: dict[str, object] = {
                "supportsAllDrives": "true",
                "pageSize": 100,
                "fields": PERMISSION_FIELDS,
            }
            if key.drive:
                params["useDomainAdminAccess"] = "true"
            try:
                perms = [
                    p
                    async for p in self.google.pages(
                        key.who,
                        "drive",
                        f"/drive/v3/files/{fid}/permissions",
                        "permissions",
                        params,
                    )
                ]
            except ApiError as e:
                if e.status not in GONE:
                    raise
                return None
            acl = acl_of(self.directory, perms, self.until())
            if acl is None:
                return None
            text, files = await self.content(key, m)
            owner = next(
                (str(o["emailAddress"]) for o in rows(m.get("owners")) if o.get("emailAddress")),
                None,
            )
            return ItemRecord(
                f"file:{fid}",
                "file",
                str(m.get("name") or fid),
                text,
                datetime.fromisoformat(str(m["modifiedTime"]).replace("Z", "+00:00")),
                container=key.name,
                container_kind="drive",
                author=owner,
                url=m.get("webViewLink"),
                payload={"mime": m.get("mimeType"), "size": int(m.get("size") or 0)},
                acl=acl,
                files=files,
            )

    async def content(self, key: Key, m: Json) -> tuple[str, list[Attachment]]:
        """The file's text (a Google format) or its bytes (for the engine to read); nothing when
        it is over the cap, a binary, or Drive refuses the read."""
        mime, fid, cap = str(m["mimeType"]), str(m["id"]), self.cfg.cap
        path = f"/drive/v3/files/{fid}"
        try:
            if mime in EXPORT:
                data = await self.google.raw(
                    key.who, "drive", path + "/export", {"mimeType": EXPORT[mime]}, cap
                )
                return (data or b"").decode("utf-8", errors="replace")[:TEXT_CAP], []
            if readable(mime) and int(m.get("size") or 0) <= cap:
                data = await self.google.raw(
                    key.who, "drive", path, {"alt": "media", "supportsAllDrives": "true"}, cap
                )
                if data:
                    return "", [Attachment(str(m["name"]), mime, data, len(data))]
        except ApiError as e:
            if e.status not in GONE:
                raise
        return "", []

    async def read(self, key: Key, files: list[Json]) -> list[ItemRecord | None]:
        done = await asyncio.gather(*(self.record(key, m) for m in files), return_exceptions=True)
        for d in done:
            if isinstance(d, BaseException):
                raise d
        return list(done)  # pyright: ignore[reportReturnType]

    async def apply(self, key: Key, fid: str, m: Json | None) -> Record | None:
        """One changed file: its record, or the removal of the one it had. A key never touches
        a file it does not hold (another key holds it), and a person's feed cannot say a file
        is gone for everyone: only a shared drive's feed removes on `removed`, and the next
        full listing prunes the rest."""
        if m is None:
            return Replaced(f"file:{fid}", "", END) if key.drive else None
        if not key.holds(m):
            return None
        if indexable(m) and await self.allowed(key, m):
            rec = await self.record(key, m)
            if rec is not None:
                return rec
        return Replaced(f"file:{fid}", "", END)


# ---- the connector ---------------------------------------------------------------------------


class GDriveConnector(Connector):
    kind: ClassVar[str] = "gdrive"
    name: ClassVar[str] = "Google Drive"
    category: ClassVar[str] = "docs"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    fields: ClassVar[tuple[Field, ...]] = (
        Field("service_account_key", "Service account key (JSON)", "secret", required=False),
        Field("domain", "Domain", placeholder="example.com"),
        Field("admin", "Admin to act as", placeholder="admin@example.com"),
        Field(
            "service_account",
            "Service account (without a key)",
            required=False,
            placeholder="reader@project.iam.gserviceaccount.com",
        ),
        Field("drives", "Shared drives", "list", required=False, placeholder="IDs; all if empty"),
        Field(
            "personal_drive",
            "People may have their own Drive read as them",
            "select",
            ("yes", "no"),
            required=False,
        ),
        Field("group", "Source group", required=False, placeholder="google"),
        Field("include_folders", "Only these folders", "list", required=False, placeholder="IDs"),
        Field("exclude_folders", "Never read", "list", required=False, placeholder="folder IDs"),
        Field("max_mb", "Largest file read, in MB", required=False, placeholder="20"),
        Field("relist_hours", "Hours between full listings", required=False, placeholder="24"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport
        self._runs: dict[object, Run] = {}

    def _google(self, source: Source) -> Google:
        return connect(source, self.transport)

    async def _open(self, source: Source, cfg: Config) -> Run:
        g = self._google(source)
        try:
            return Run(cfg, g, await read_directory(g, cfg.domain, cfg.admin))
        except BaseException:
            await g.aclose()
            raise

    async def _run(self, source: Source) -> Run:
        """The directory, read once per run (the pass reads it; accounts and content reuse it)."""
        if (run := self._runs.get(source.id)) is None:
            run = self._runs[source.id] = await self._open(source, Config.of(source.config))
        return run

    async def release(self, source: Source) -> None:
        if run := self._runs.pop(source.id, None):
            await run.google.aclose()

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            run = await self._open(source, Config.of(source.config))
        except (ValueError, Upstream) as e:
            return False, str(e)
        try:
            keys = await run.keys()
        except Upstream as e:
            return False, str(e)
        finally:
            await run.google.aclose()
        drives = sum(1 for k in keys if k.drive)
        return True, f"{len(run.directory.active)} people, {drives} shared drives to read"

    recheck_reads: ClassVar[bool] = True

    def workspace_of(self, source: Source) -> str:
        return Config.of(source.config).domain

    async def can_open(
        self, source: Source, external_id: str, subjects: Sequence[str]
    ) -> bool | None:
        """Whether Drive lets this person (their directory user ids) open the file now: each
        read at the source, the live re-check and the parity oracle ask here. None: no
        subject names an active person to act as."""
        cfg = Config.of(source.config)
        g = self._google(source)
        try:
            emails = [e for i in subjects if (e := await person_email(g, cfg.admin, i))]
            if not emails:
                return None
            return any(
                [await self.can_open_as(g, external_id.removeprefix("file:"), e) for e in emails]
            )
        finally:
            await g.aclose()

    async def my_drive_search(
        self, source: Source, user_id: str, query: str, limit: int
    ) -> list[LiveHit]:
        """Files of this person's own My Drive that match `query`, read as them now (Docs,
        Slides and Sheets with their text); never stored. Empty when the source does not allow
        it or the directory does not list them as active."""
        cfg = Config.of(source.config)
        if not cfg.personal_drive:
            return []
        g = self._google(source)
        try:
            email = await person_email(g, cfg.admin, user_id)
            if email is None:
                return []
            words = query.replace("\\", "\\\\").replace("'", "\\'")
            listed = await g.get(
                email,
                "drive",
                "/drive/v3/files",
                {
                    "corpora": "user",
                    "q": f"'me' in owners and trashed = false and fullText contains '{words}'",
                    "pageSize": min(max(limit, 1), 25),
                    "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
                },
            )
            hits: list[LiveHit] = []
            for f in rows(listed.get("files")):
                mime, text = str(f.get("mimeType") or ""), ""
                if mime in EXPORT:
                    raw = await g.raw(
                        email,
                        "drive",
                        f"/drive/v3/files/{f['id']}/export",
                        {"mimeType": EXPORT[mime]},
                        MY_DRIVE_TEXT,
                    )
                    text = (raw or b"").decode("utf-8", "replace")
                hits.append(
                    LiveHit(
                        f"file:{f['id']}",
                        str(f.get("name") or ""),
                        text,
                        "file",
                        str(f["webViewLink"]) if f.get("webViewLink") else None,
                        None,
                        datetime.fromisoformat(str(f["modifiedTime"]).replace("Z", "+00:00"))
                        if f.get("modifiedTime")
                        else None,
                    )
                )
            return hits
        finally:
            await g.aclose()

    @staticmethod
    async def can_open_as(g: Google, file_id: str, email: str) -> bool:
        """Whether Drive lets the person with this address open the file (they are the one
        acting). A refusal, or a person who cannot be acted as, is no."""
        try:
            await g.get(
                email.lower(),
                "drive",
                f"/drive/v3/files/{file_id}",
                {"supportsAllDrives": "true", "fields": "id"},
            )
            return True
        except DelegationRefused:
            return False
        except ApiError as e:
            if e.status in GONE:
                return False
            raise

    # ---- who reads what ----

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit, complete every run: a group per directory group, empty ones included (a group
        missing from the batch loses all its members), and the domain's people."""
        d = (await self._run(source)).directory
        groups = {
            group_subject(d.domain, email): tuple(user_subject(d.domain, i) for i in ids)
            for email, ids in d.groups.items()
        }
        groups[members_subject(d.domain)] = tuple(
            user_subject(d.domain, i) for i in sorted(d.active.values())
        )
        yield PermissionUnit(
            "pass", f"{d.domain}:groups", PASS_MINUTES, groups, time.time_ns() // 1000
        )

    async def accounts(self, source: Source) -> Roster | None:
        """The domain's people; Google verifies the address it lists."""
        d = (await self._run(source)).directory
        people = tuple(
            SourceAccount(
                i,
                str(obj(u.get("name")).get("fullName") or ""),
                str(u["primaryEmail"]).lower(),
                True,
                not (u.get("suspended") or u.get("archived")),
            )
            for i, u in d.people.items()
        )
        return Roster(PROVIDER, d.domain, people)

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        run = await self._run(source)
        keys = await run.keys()
        old: Json = source.state or {}
        feeds = {str(k): str(v) for k, v in obj(old.get("feeds")).items()}
        due = run.started - datetime.fromisoformat(
            old.get("full_at") or "1970-01-01T00:00:00+00:00"
        )
        if old.get("v") == 1 and set(feeds) == {k.id for k in keys} and due < run.cfg.relist:
            try:
                async for rec in self._changes(run, keys, old, feeds):
                    yield rec
                return
            except Expired:
                log.info("drive.feed_expired", source=str(source.id))
        async for rec in self._everything(run, keys, old):
            yield rec

    async def _everything(self, run: Run, keys: list[Key], old: Json) -> AsyncGenerator[Record]:
        """Every file of every scope again, with the feeds' starting points taken first so that
        a change made while listing is read next run. What was not listed is pruned."""
        starts = {k.id: await run.start_token(k) for k in keys}
        live: set[str] = set()
        for key in keys:
            files: list[Json] = []
            async for m in run.listing(key):
                run.parents[str(m["id"])] = list(m.get("parents") or [])
                files.append(m)
            wanted = [m for m in files if indexable(m) and await run.allowed(key, m)]
            for i in range(0, len(wanted), BATCH):
                for rec in await run.read(key, wanted[i : i + BATCH]):
                    if rec:
                        live.add(rec.external_id)
                        yield rec
                yield Checkpoint(old)
        state: Json = {"v": 1, "full_at": run.started.isoformat(), "feeds": starts}
        yield SyncState(state, live)

    async def _changes(
        self, run: Run, keys: list[Key], old: Json, feeds: dict[str, str]
    ) -> AsyncGenerator[Record]:
        for key in keys:
            while True:
                params = run.feed_params(key) | {
                    "pageToken": feeds[key.id],
                    "pageSize": 200,
                    "fields": CHANGE_FIELDS,
                }
                try:
                    page = await run.google.get(key.who, "drive", "/drive/v3/changes", params)
                except ApiError as e:
                    if e.status in (400, 410):
                        raise Expired from None
                    raise
                changed: dict[str, Json | None] = {
                    str(c["fileId"]): None if c.get("removed") else obj(c.get("file"))
                    for c in rows(page.get("changes"))
                }
                for fid, m in changed.items():
                    if (rec := await run.apply(key, fid, m)) is not None:
                        yield rec
                feeds[key.id] = str(page.get("nextPageToken") or page["newStartPageToken"])
                yield Checkpoint({**old, "feeds": dict(feeds)})
                if not page.get("nextPageToken"):
                    break
        yield SyncState({**old, "feeds": feeds})
