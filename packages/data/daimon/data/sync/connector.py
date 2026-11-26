"""The contract every connector implements, and the records one yields.

A connector reads one kind of source and yields what it finds; the engine (`engine.py`)
decides scope, chunks and stores it, and keeps the cursor. A connector never writes the
store. Registered with `register`, looked up by `sources.kind`.

A connector on the access list model sets `descriptor` and gives each record the `Acl` its
source states; an unlisted record is read by nobody. A connector without one gives records the
scope its source's rules or default name.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Final
from uuid import UUID

import psycopg.conninfo
from daimon.data.identity import SourceAccount
from daimon.data.sync.rate import RateBudget

Json = dict[str, Any]


@dataclass(frozen=True)
class Field:
    """One setting of a connector's configuration form."""

    name: str
    label: str
    kind: str = "text"  # text | secret | select | list
    options: tuple[str, ...] = ()
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class Descriptor:
    """What a connector on the access list model declares: the namespace of its subjects."""

    provider: str


@dataclass(frozen=True)
class Person:
    """One person's sign-in at a source (`connect.py`). `token` is their live access token;
    None when the connection no longer proves itself (a refused refresh), so its access ends."""

    account_id: UUID
    subject: str
    token: str | None


@dataclass(frozen=True)
class Source:
    """What a connector is given of one source: its row, its opened credential and its cursor."""

    id: UUID
    tenant_id: UUID
    kind: str
    name: str
    provider: str
    config: Json
    credentials: dict[str, str]
    default_scope: str
    state: Json
    last_sync_at: datetime | None
    budget: RateBudget
    private_hosts: tuple[str, ...] = ()
    keyless_accounts: tuple[str, ...] = ()
    """The service accounts the deployment lets this source sign as without a key."""
    deadline: float | None = None
    """Monotonic time after which the run stops at its next checkpoint."""
    people: tuple[Person, ...] = ()
    """The people signed in at this source; set only for a connector with `person_sign_in`."""


@dataclass(frozen=True)
class Roster:
    """A source's whole account listing for one workspace (see `identity.record_accounts`)."""

    provider: str
    workspace: str
    accounts: tuple[SourceAccount, ...]


@dataclass
class Attachment:
    """A file of a record. `data` is what the connector read of it (None: listed only); the
    engine reads its text and keeps neither the bytes nor more than its size."""

    name: str
    content_type: str = ""
    data: bytes | None = None
    size: int = 0


@dataclass(frozen=True)
class AclRef:
    ref: str
    """A subject, or a group's name."""
    group: bool = False
    unit: tuple[str, str] | None = None
    """(kind, ref) of the evidence unit a direct ref's grant rides on."""


@dataclass(frozen=True)
class Acl:
    """Who may read one record: a leaf (`refs`) or a conjunction (`all_of`: two or more
    leaves, every one required)."""

    refs: tuple[AclRef, ...] = ()
    all_of: tuple[Acl, ...] = ()
    personal: bool = False
    """One person's private record."""
    until: datetime | None = None
    """The record's own evidence ends here unless a later read renews it."""


@dataclass(frozen=True)
class PermissionUnit:
    """One unit of a permission pass. `groups` is the unit's complete batch `{group: [subjects]}`,
    empty groups included. `seq` orders passes of a unit: an older one is discarded.
    `fetched_at` is when the evidence was read (None: the run's start); the unit lives
    `minutes` from it."""

    kind: str
    ref: str
    minutes: int
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    seq: int = 0
    fetched_at: datetime | None = None


@dataclass
class ItemRecord:
    """One record: a message window, a document, a meeting. `external_id` is `<kind>:<id>`."""

    external_id: str
    kind: str
    title: str
    body: str
    occurred_at: datetime
    container: str | None = None
    container_kind: str = "channel"
    author: str | None = None
    client: str | None = None
    url: str | None = None
    payload: Json = field(default_factory=Json)
    facets: Json = field(default_factory=Json)
    """Extra fields the source's scope rules may match, with `container` and `client`."""
    acl: Acl | None = None
    files: list[Attachment] = field(default_factory=list[Attachment])


@dataclass
class Checkpoint:
    """Everything yielded so far is complete: the engine may commit it with `state`, and a run
    cut by its budget or a failure resumes from there."""

    state: Json


@dataclass
class SyncState:
    """The end of a complete pass: the ids that exist upstream (every other record of the
    source is deleted) and the cursor saved on success. `live` None keeps everything."""

    state: Json
    live: set[str] | None = None
    trust_empty: bool = False
    """A `live` that is empty while records are stored is refused, as a wrong database or a
    revoked grant looks like it; set this when empty is the truth."""
    keep: tuple[str, ...] = ()
    """Prefixes of ids a `live` pass keeps though unlisted: a container lost for now."""


@dataclass
class Replaced:
    """Records with ids from `prefix + start` up to `prefix + end` (or through the end of
    `prefix`) were rebuilt upstream: those not in `keep` are gone."""

    prefix: str
    start: str
    end: str | None = None
    keep: set[str] = field(default_factory=set[str])


Record = ItemRecord | Checkpoint | SyncState | Replaced


@dataclass(frozen=True)
class LiveHit:
    """One result of a live source's search, read now and never stored. `ref` is its id in
    the source."""

    ref: str
    title: str
    text: str = ""
    kind: str = "item"
    url: str | None = None
    author: str | None = None
    occurred_at: datetime | None = None
    detail: str = ""
    """More text, kept only when every reader's hit carries the same (a Toggl admin's rates)."""


class Connector:
    """Base of every connector. Override what the kind needs; the defaults do nothing."""

    kind: ClassVar[str]
    name: ClassVar[str]
    category: ClassVar[str] = ""  # chat | meetings | docs | code | crm | time | knowledge
    fields: ClassVar[tuple[Field, ...]] = ()
    default_scope: ClassVar[str | None] = None
    descriptor: ClassVar[Descriptor | None] = None
    person_sign_in: ClassVar[bool] = False
    """The engine gives a run the people signed in at the source (`Source.people`)."""

    def workspace_of(self, source: Source) -> str:
        """The namespace of this source's subjects (`config.workspace`, else the source);
        a connector that keys its accounts by something else (a domain) overrides it."""
        return str(source.config.get("workspace") or source.id)

    def descriptor_for(self, source: Source) -> Descriptor | None:
        """Whether this source's records are on the access list model; a connector whose
        sources differ (a readers column set or not) overrides it."""
        return self.descriptor

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        """The records changed since `source.state`, with checkpoints, ending in a `SyncState`."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """The source's permission pass, one unit at a time, before its content. None by default."""
        return
        yield  # pragma: no cover

    async def accounts(self, source: Source) -> Roster | None:
        """The workspace's users, after the permission pass and before content. None by default."""
        return None

    @classmethod
    def has_permissions(cls) -> bool:
        return cls.permissions is not Connector.permissions

    async def fetch_file(
        self, source: Source, payload: Json, message: str, name: str, index: int = 0
    ) -> tuple[bytes, str] | None:
        """The file `name` attached to `message`, read now with the source's own credential:
        (bytes, content type). None when the source no longer shows it or it is too large.
        Nothing is kept."""
        return None

    async def release(self, source: Source) -> None:
        """End of a run: close what the connector kept open for it."""

    async def test(self, source: Source) -> tuple[bool, str]:
        return True, "ok"

    live: ClassVar[bool] = False
    """A source of this kind is read upstream at read time (`mode` federated), never stored."""
    recheck_reads: ClassVar[bool] = False
    """Each stored record is checked with `can_open` before it is shown."""

    async def live_search(self, source: Source, query: str, limit: int) -> list[LiveHit]:
        """Live kinds: what the source finds for `query` now. Read-only; nothing is kept."""
        raise NotImplementedError

    live_as_reader: ClassVar[bool] = False
    """A live source read as each reader, with their own access (`live_search_as`); it
    answers only a caller who names readers, and a hit shows only if every reader's read has it."""

    async def live_search_as(
        self, source: Source, query: str, limit: int, subjects: Sequence[str]
    ) -> list[LiveHit]:
        """Live kinds read as one person, given their subjects (bare ids in the source's own
        namespace): what that person's own access finds for `query` now."""
        raise NotImplementedError

    async def can_open(
        self, source: Source, external_id: str, subjects: Sequence[str]
    ) -> bool | None:
        """Whether the person holding these source subjects (bare ids in the source's own
        namespace) may open the record in the source itself, asked now. None: cannot say.
        Serves the read-time re-check and the parity oracle; the default offers neither."""
        raise NotImplementedError

    @classmethod
    def has_can_open(cls) -> bool:
        return cls.can_open is not Connector.can_open

    def scan_key(self, source: Source) -> str | None:
        """A sync that reads an external database returns that database's key (`dsn_key`): runs
        of sources with the same key never overlap. HTTP APIs return None and run side by side."""
        return None


CONNECTORS: Final[dict[str, Connector]] = {}


def register(connector: Connector) -> Connector:
    """Add `connector` to the registry under its kind; one kind has one connector."""
    if connector.kind in CONNECTORS:
        raise ValueError(f"connector kind {connector.kind} is already registered")
    CONNECTORS[connector.kind] = connector
    return connector


def cursor_at(cursor: str | None) -> tuple[datetime, str] | None:
    """Keyset cursors are `<iso time>|<id>`."""
    when, _, key = (cursor or "").partition("|")
    try:
        return (datetime.fromisoformat(when), key) if when and key else None
    except ValueError:
        return None


def dsn_key(dsn: str | None) -> str | None:
    """Which external database a Postgres URL reads, from its host, port and database name
    only (never user or password). None: not a usable URL."""
    try:
        parts = psycopg.conninfo.conninfo_to_dict(dsn or "")
    except psycopg.ProgrammingError:
        return None
    host = parts.get("host") or parts.get("hostaddr")
    if not host:
        return None
    ident = f"{host}:{parts.get('port', 5432)}/{parts.get('dbname', '')}"
    return hashlib.sha256(ident.encode()).hexdigest()
