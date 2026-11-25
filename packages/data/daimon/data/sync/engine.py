"""One sync run: pull a source's records from its connector, place, chunk and store them.

A run holds its source's lock throughout. The connector yields records and checkpoints; the
engine writes records in batches (an idempotent upsert, so a record already stored and
unchanged is left alone), and saves the cursor with each checkpoint's batch, so a run cut by
its budget or a failure resumes from the last checkpoint. A complete pass ends with the ids
that exist upstream, and the rest of the source's records are deleted.

The engine writes as the store's owner. A record on the access list model is stored with the
restricted scope and given its list by `acl.set_records` in the same transaction, so it is
never readable by scope.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Collection, Iterable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

import psycopg
import structlog
from daimon.core.config import DataSettings, Settings
from daimon.data import acl, audit, connect, identity, redaction
from daimon.data.access import ACL_SCOPE
from daimon.data.locks import advisory_key
from daimon.data.metering import Meter
from daimon.data.schema import Schemas
from daimon.data.sync.attachments import text_of
from daimon.data.sync.chunking import chunks, clean, context, resolve_scope
from daimon.data.sync.connector import (
    CONNECTORS,
    Acl,
    Checkpoint,
    Connector,
    ItemRecord,
    Json,
    PermissionUnit,
    Replaced,
    Source,
    SyncState,
)
from daimon.data.sync.credentials import unseal
from daimon.data.sync.embedding import embed_pending
from daimon.data.sync.http import GuardedTransport
from daimon.data.sync.rate import RateBudget
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

log = structlog.get_logger()

BATCH: Final = 200
"""Records written in one transaction."""
COMMIT_EVERY: Final = 15.0
"""Seconds between checkpoint commits."""
REFUSED: Final = (psycopg.DataError, psycopg.errors.ProgramLimitExceeded, UnicodeError)
"""What Postgres refuses of one record: it is skipped and counted, not the run's end."""
ERROR_CHARS: Final = 500
STALE_RUN: Final = timedelta(minutes=2)
"""A running row older than this whose source lock is free was cut by a restart."""


@dataclass(frozen=True)
class Env:
    """What a run needs of the deployment, taken once from settings."""

    schemas: Schemas
    crypto_keys: tuple[str, ...] = ()
    private_hosts: tuple[str, ...] = ()
    keyless_accounts: tuple[str, ...] = ()
    """`workspace=account` entries: the service accounts a workspace's Google sources may sign
    as without a key."""
    embed: bool = False
    clients: Mapping[str, connect.Client] = field(default_factory=dict[str, connect.Client])
    """Daimon's own sign-in clients, for refreshing a connected person's token."""
    data: DataSettings | None = None
    """The settings embedding reads; with `embed`, always set by `of`."""
    meter: Meter | None = None
    """Bills embeddings to the source's tenant; none: embeds unmetered."""

    @classmethod
    def of(cls, settings: Settings) -> Env:
        return cls(
            Schemas.of(settings.data),
            tuple(k.get_secret_value() for k in settings.crypto.keys),
            settings.data.private_hosts,
            settings.data.google_keyless_accounts,
            settings.data.embed_url is not None,
            connect.configured_clients(settings.data),
            settings.data,
        )


@dataclass(frozen=True)
class RunResult:
    run_id: UUID | None
    status: str
    """`ok`, `partial` (budget spent: the next run continues), `error`, or `skipped`."""
    added: int = 0
    updated: int = 0
    deleted: int = 0
    error: str | None = None


class _Busy(Exception):
    """Another run reads the same external database."""


@dataclass
class _Counts:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    partial: bool = False
    state: Json | None = None
    sync: SyncState | None = None


def record_hash(r: ItemRecord) -> str:
    """Of what the chunks are made from: a change to any of it rewrites the record's chunks."""
    files = [
        (f.name, f.content_type, f.size or len(f.data or b""), hashlib.sha256(f.data).hexdigest())
        if f.data
        else (f.name, f.content_type, f.size, None)
        for f in r.files
    ]
    parts = [r.kind, r.title, r.body, r.url, r.author, r.container, r.container_kind]
    parts += [r.occurred_at.isoformat(), files]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


async def _one(
    conn: AsyncConnection[DictRow], sql: str, params: Iterable[object] | dict[str, object] = ()
) -> DictRow | None:
    return await (await conn.execute(sql.encode(), params)).fetchone()  # pyright: ignore[reportArgumentType]


async def try_lock(conn: AsyncConnection[DictRow], key: int) -> bool:
    row = await _one(conn, "select pg_try_advisory_lock(%s) as ok", (key,))
    return bool(row and row["ok"])


async def unlock(conn: AsyncConnection[DictRow], key: int) -> None:
    await conn.execute(b"select pg_advisory_unlock(%s)", (key,))


async def _noop() -> None:
    """A transaction that failed has rolled back by itself."""


class _Run:
    """The work of one run on one connection."""

    def __init__(
        self,
        conn: AsyncConnection[DictRow],
        env: Env,
        source: Source,
        connector: Connector,
        read_at: datetime,
        redactor: redaction.Redactor | None = None,
    ) -> None:
        self.conn, self.env, self.src, self.connector, self.read_at = (
            conn,
            env,
            source,
            connector,
            read_at,
        )
        self.redactor = redactor
        self.scopes: dict[str, str] = {}
        self.dropped: set[str] = set()
        self.d = env.schemas.data
        self.acl_model = connector.descriptor_for(source) is not None
        self.c = _Counts()
        self.pending: dict[str, ItemRecord] = {}
        self._acl_ids: dict[Acl, int] = {}
        self._committed = time.monotonic()

    # ---- the pass -----------------------------------------------------------------------------

    async def permissions(self) -> None:
        """The source's permission pass: each unit is one evidence publication."""
        if not self.connector.has_permissions():
            return
        async with aclosing(self.connector.permissions(self.src)) as units:
            async for unit in units:
                await acl.retry(lambda u=unit: self._publish(u), _noop)

    async def _publish(self, unit: PermissionUnit) -> None:
        async with self.conn.transaction():
            await acl.lock(self.conn, self.env.schemas)
            unit_id = await acl.unit(self.conn, self.env.schemas, self.src.id, unit.kind, unit.ref)
            await acl.publish(
                self.conn,
                self.env.schemas,
                unit_id,
                unit.seq,
                {g: list(members) for g, members in unit.groups.items()},
                unit.fetched_at,
                timedelta(minutes=unit.minutes),
            )

    async def identities(self) -> None:
        """The source's users, recorded as the identities of its workspace."""
        roster = await self.connector.accounts(self.src)
        if roster is not None:
            await identity.record_accounts(
                self.conn,
                self.env.schemas,
                tenant_id=self.src.tenant_id,
                provider=roster.provider,
                workspace=roster.workspace,
                accounts=roster.accounts,
                auto_link_domain=self.src.config.get("auto_link_domain"),
            )

    async def content(self) -> None:
        async with aclosing(self.connector.fetch(self.src)) as records:
            async for rec in records:
                rec = clean(rec)
                if isinstance(rec, ItemRecord) and self.redactor:
                    if self.redactor.excludes(rec.external_id):
                        self.dropped.add(rec.external_id)
                        continue
                    self.scopes[rec.external_id] = self._scope(rec)  # from what the source said
                    rec = self.redactor.record(rec)
                if isinstance(rec, ItemRecord):
                    self.pending[rec.external_id] = rec
                    if len(self.pending) >= BATCH:
                        await self.flush()
                elif isinstance(rec, Checkpoint):
                    if await self.checkpoint(rec):
                        return
                elif isinstance(rec, Replaced):
                    await self.flush()
                    await self.replaced(rec)
                else:
                    self.c.sync = rec

    async def checkpoint(self, rec: Checkpoint) -> bool:
        """Commit now and then; past the deadline commit and stop (True)."""
        now = time.monotonic()
        late = self.src.deadline is not None and now > self.src.deadline
        if late or now - self._committed >= COMMIT_EVERY:
            await self.flush(rec.state)
            self._committed = time.monotonic()
        self.c.state = rec.state
        self.c.partial = late
        return late

    async def finish(self) -> None:
        """The last batch, then the prune and the final cursor in one transaction."""
        sync = self.c.sync
        await self.flush(sync.state if sync else self.c.state)
        if sync and sync.live is not None:
            await self.prune(sync)

    # ---- writes -------------------------------------------------------------------------------

    async def flush(self, state: Json | None = None) -> None:
        """Write the pending records in one transaction (a deadlock or serialization failure
        runs it again), with the cursor when there is one. Attachment text is read before it."""
        batch, self.pending = list(self.pending.values()), {}
        if self.dropped:
            await self.conn.execute(
                f"delete from {self.d}.records where source_id = %s"
                " and external_id = any(%s)".encode(),
                (self.src.id, sorted(self.dropped)),
            )
            self.dropped = set()
        if not batch and state is None:
            return
        known = await self._known([r.external_id for r in batch])
        texts = {
            r.external_id: await self._texts(r)
            for r in batch
            if known.get(r.external_id) != record_hash(r)
        }
        counts = await acl.retry(lambda: self._batch(batch, texts, state), self._forget_acls)
        self.c.added += counts[0]
        self.c.updated += counts[1]
        self.c.skipped += counts[2]
        if counts[2]:
            log.warning("sync.records_skipped", source=str(self.src.id), skipped=counts[2])

    async def _forget_acls(self) -> None:
        """A rolled-back batch may have interned access lists that never committed."""
        self._acl_ids.clear()

    async def _known(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        cur = await self.conn.execute(
            f"select external_id, hash from {self.d}.records where source_id = %s"
            " and external_id = any(%s)".encode(),
            (self.src.id, ids),
        )
        return {r["external_id"]: r["hash"] for r in await cur.fetchall()}

    async def _texts(self, r: ItemRecord) -> list[str | None]:
        texts = [await text_of(f.name, f.content_type, f.data) if f.data else None for f in r.files]
        if self.redactor:
            texts = [t and self.redactor.text(t) for t in texts]
        return texts

    async def _batch(
        self, batch: list[ItemRecord], texts: dict[str, list[str | None]], state: Json | None
    ) -> tuple[int, int, int]:
        added = updated = skipped = 0
        access: list[acl.RecordAccess] = []
        async with self.conn.transaction():
            if any(r.acl for r in batch):
                await acl.lock(self.conn, self.env.schemas)
            for rec in batch:
                try:
                    async with self.conn.transaction():
                        wrote = await self._write(rec, texts.get(rec.external_id))
                except REFUSED:
                    skipped += 1
                    continue
                added += wrote is True
                updated += wrote is False
                if self.acl_model and rec.acl is not None:
                    access.append(
                        {
                            "ext": rec.external_id,
                            "acl": await self._acl_id(rec.acl),
                            "until": rec.acl.until,
                        }
                    )
            if access:
                await acl.set_records(
                    self.conn, self.env.schemas, self.src.id, access, self.read_at
                )
            if state is not None:
                await self.conn.execute(
                    f"update {self.d}.source_cursors set state = %s where source_id = %s".encode(),
                    (Jsonb(state), self.src.id),
                )
        return added, updated, skipped

    async def _write(self, r: ItemRecord, texts: list[str | None] | None) -> bool | None:
        """Upsert one record; True added, False changed, None left as it was."""
        scope = self._scope(r)
        digest = record_hash(r)
        payload = {**r.payload}
        if r.client:
            payload["_client"] = r.client
        if r.facets:
            payload["_facets"] = r.facets
        if r.container:
            payload["_container"] = {"name": r.container, "kind": r.container_kind}
        row = await _one(
            self.conn,
            f"with old as (select hash from {self.d}.records where source_id = %(src)s"
            " and external_id = %(ext)s)"
            f" insert into {self.d}.records (source_id, external_id, kind, title, body, url,"
            " author, occurred_at, scope, payload, hash)"
            " values (%(src)s, %(ext)s, %(kind)s, %(title)s, %(body)s, %(url)s, %(author)s,"
            " %(at)s, %(scope)s, %(payload)s, %(hash)s)"
            " on conflict (source_id, external_id) do update set kind = excluded.kind,"
            " title = excluded.title, body = excluded.body, url = excluded.url,"
            " author = excluded.author, occurred_at = excluded.occurred_at,"
            " scope = excluded.scope, payload = excluded.payload, hash = excluded.hash,"
            " ingested_at = now()"
            f" where {self.d}.records.hash is distinct from excluded.hash"
            f" or {self.d}.records.scope is distinct from excluded.scope"
            f" or {self.d}.records.payload is distinct from excluded.payload"
            " returning id, (xmax = 0) as inserted, (select hash from old) as before",
            {
                "src": self.src.id,
                "ext": r.external_id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "url": r.url,
                "author": r.author,
                "at": r.occurred_at,
                "scope": scope,
                "payload": Jsonb(payload),
                "hash": digest,
            },
        )
        if row is None:
            return None
        if row["inserted"] or row["before"] != digest:
            await self._chunks(row["id"], r, texts or [])
        return bool(row["inserted"])

    def _scope(self, r: ItemRecord) -> str:
        """The restricted scope for a record on the access list model, else the source's rules."""
        if r.external_id in self.scopes:
            return self.scopes[r.external_id]
        if self.acl_model:
            return ACL_SCOPE
        facets = {"container": r.container, "client": r.client, **r.facets}
        rules: list[dict[str, str]] = self.src.config.get("scope_rules") or []
        return resolve_scope(rules, self.src.default_scope, facets)

    async def _chunks(self, record_id: UUID, r: ItemRecord, texts: list[str | None]) -> None:
        ctx = context(r.occurred_at, self.src.name, r.container, r.container_kind, r.author)
        pieces = chunks(r.title, r.body)
        for f, text in zip(r.files, texts, strict=False):
            if text:
                pieces += chunks(f.name, text)
        scope = self._scope(r)
        await self.conn.execute(
            f"delete from {self.d}.chunks where record_id = %s".encode(), (record_id,)
        )
        await self.conn.execute(
            f"delete from {self.d}.attachments where source_id = %s and external_id = %s".encode(),
            (self.src.id, r.external_id),
        )
        async with self.conn.cursor() as cur:
            await cur.executemany(
                f"insert into {self.d}.chunks (record_id, source_id, ord, text, ctx, scope)"
                " values (%s, %s, %s, %s, %s, %s)".encode(),
                [(record_id, self.src.id, i, t, ctx, scope) for i, t in enumerate(pieces)],
            )
            await cur.executemany(
                f"insert into {self.d}.attachments (source_id, external_id, ord, name,"
                " content_type, size, extracted) values (%s, %s, %s, %s, %s, %s, %s)".encode(),
                [
                    (
                        self.src.id,
                        r.external_id,
                        i,
                        f.name,
                        f.content_type,
                        f.size or len(f.data or b""),
                        bool(t),
                    )
                    for i, (f, t) in enumerate(
                        zip(r.files, texts or [None] * len(r.files), strict=True)
                    )
                ],
            )

    async def _acl_id(self, a: Acl) -> int:
        """The id of an access list definition, made on first use. A list with no entry reads
        as nobody (id 0)."""
        key = replace(a, until=None)
        if key not in self._acl_ids:
            self._acl_ids[key] = await self._intern(a)
        return self._acl_ids[key]

    async def _intern(self, a: Acl) -> int:
        schemas, src = self.env.schemas, self.src.id
        if a.all_of:
            kids = [await self._acl_id(k) for k in a.all_of]
            return await acl.intern(self.conn, schemas, src, all_of=kids, personal=a.personal)
        if not a.refs:
            return 0
        refs: list[acl.Ref] = []
        for ref in a.refs:
            unit = await acl.unit(self.conn, schemas, src, *ref.unit) if ref.unit else None
            refs.append(acl.ref(ref.ref, group=ref.group, unit_id=unit))
        return await acl.intern(self.conn, schemas, src, refs, personal=a.personal)

    async def replaced(self, r: Replaced) -> None:
        res = await self.conn.execute(
            f"delete from {self.d}.records where source_id = %s and starts_with(external_id, %s)"
            " and external_id >= %s and (%s::text is null or external_id < %s)"
            " and external_id <> all(%s)".encode(),
            (
                self.src.id,
                r.prefix,
                r.prefix + r.start,
                r.end,
                r.prefix + (r.end or ""),
                list(r.keep),
            ),
        )
        self.c.deleted += res.rowcount

    async def prune(self, sync: SyncState) -> None:
        """Delete the source's records that are not upstream any more. An empty list while
        records are stored is refused: a wrong database or a revoked grant looks like it."""
        live = sorted(sync.live or ())
        if not live and not sync.trust_empty:
            row = await _one(
                self.conn,
                f"select count(*) as n from {self.d}.records where source_id = %s",
                (self.src.id,),
            )
            if row and row["n"]:
                raise EmptySnapshot(
                    f"the source lists nothing but {row['n']} records are stored: nothing was"
                    " removed. A revoked grant or a wrong database looks like this"
                )
        async with self.conn.transaction():
            res = await self.conn.execute(
                f"delete from {self.d}.records where source_id = %s and external_id in ("
                f"select external_id from {self.d}.records where source_id = %s"
                " except select unnest(%s::text[]))".encode(),
                (self.src.id, self.src.id, live),
            )
            self.c.deleted += res.rowcount
            await self.conn.execute(
                f"delete from {self.d}.record_access a where a.source_id = %s and not exists ("
                f"select 1 from {self.d}.records r where r.source_id = a.source_id"
                " and r.external_id = a.external_id)".encode(),
                (self.src.id,),
            )


class EmptySnapshot(RuntimeError):
    """A connector said nothing exists upstream while records are stored."""


DEFAULT_RATE: Final = 60.0
"""Upstream calls per minute a source may make unless `rate_per_minute` says otherwise
(0 is unlimited)."""


def build_source(row: DictRow, env: Env, deadline: float | None) -> Source:
    config: Json = row["config"] or {}
    set_rate = config.get("rate_per_minute")
    rate = DEFAULT_RATE if set_rate in (None, "") else float(set_rate)
    source = Source(
        id=row["id"],
        tenant_id=row["tenant_id"],
        kind=row["kind"],
        name=row["name"],
        provider=row["provider"] or row["kind"],
        config=config,
        credentials=unseal(env.crypto_keys, row["credential"]),
        default_scope=row["default_scope"],
        state=row["state"] or {},
        last_sync_at=row["last_sync_at"],
        budget=RateBudget(rate, burst=rate),
        private_hosts=env.private_hosts,
        deadline=deadline,
    )
    return source


async def _keyless(conn: AsyncConnection[DictRow], env: Env, source: Source) -> tuple[str, ...]:
    """The service accounts the deployment lets this source's Daimon workspace sign as without a
    key: entries `<workspace>=<account>`, the workspace a tenant id or `<platform>:<id>` (as in
    the declared file). Never keyed by anything a source's own config says."""
    found: list[str] = []
    for entry in env.keyless_accounts:
        ws, _, account = entry.partition("=")
        ws = ws.strip()
        try:
            same = UUID(ws) == source.tenant_id
        except ValueError:
            platform, _, ext = ws.partition(":")
            cur = await conn.execute(
                b"select 1 from tenants where id = %s and platform = %s and external_id = %s",
                (source.tenant_id, platform, ext),
            )
            same = await cur.fetchone() is not None
        if same and account.strip():
            found.append(account.strip().lower())
    return tuple(found)


async def ready(
    conn: AsyncConnection[DictRow],
    env: Env,
    source: Source,
    accounts: Collection[UUID] | None = None,
) -> Source:
    """The source with what needs the store: the people signed in at it (those of `accounts`
    only, when given), when its connector reads as each person, and the keyless accounts its
    workspace may sign as."""
    connector = CONNECTORS.get(source.kind)
    if connector is None:
        return source
    if connector.person_sign_in:
        people = await connect.connected_people(
            conn,
            env.schemas,
            env.crypto_keys,
            env.clients,
            GuardedTransport(env.private_hosts),
            source.id,
            accounts,
        )
        source = replace(source, people=people)
    if env.keyless_accounts:
        source = replace(source, keyless_accounts=await _keyless(conn, env, source))
    return source


_LOAD: Final = (
    "select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
    " s.default_scope, s.status, s.publication, s.redaction_set, c.state, c.last_sync_at,"
    " c.last_status, c.errors from {d}.sources s"
    " left join {d}.source_cursors c on c.source_id = s.id"
    " where s.id = %s"
)


async def run_source(
    conn: AsyncConnection[DictRow],
    env: Env,
    source_id: UUID,
    trigger: str = "schedule",
    budget: float | None = None,
) -> RunResult:
    """One run of one source. `budget`: seconds before it stops at its next checkpoint, as
    `partial` (the cursor is kept; `last_sync_at` moves only on a complete run). A source
    already syncing, or reading a database another run reads, is skipped."""
    d = env.schemas.data
    lock = advisory_key(d, f"source:{source_id}")
    if not await try_lock(conn, lock):
        return RunResult(None, "skipped", error="already syncing")
    try:
        return await _locked(conn, env, source_id, trigger, budget)
    finally:
        await unlock(conn, lock)


async def _locked(
    conn: AsyncConnection[DictRow], env: Env, source_id: UUID, trigger: str, budget: float | None
) -> RunResult:
    d = env.schemas.data
    row = await _one(conn, _LOAD.format(d=d), (source_id,))
    if row is None:
        return RunResult(None, "skipped", error="no such source")
    connector = CONNECTORS.get(row["kind"])
    skip = (
        "disabled"
        if row["status"] == "disabled"
        else "retired"
        if row["publication"] == "retired"
        else None
        if connector
        else "unknown connector"
    )
    if skip or connector is None:
        if skip in ("disabled", "retired"):
            await conn.execute(
                f"update {d}.source_cursors set requested_at = null where source_id = %s".encode(),
                (source_id,),
            )
        return RunResult(None, "skipped", error=skip)
    redactor = None
    if row["redaction_set"]:
        try:
            redactor = await redaction.ingest_redactor(
                conn, env.schemas, row["tenant_id"], row["redaction_set"]
            )
        except redaction.NotApproved as e:
            await redaction.hold(conn, env.schemas, source_id, str(e))
            return RunResult(None, "skipped", error=str(e))
    started = await _one(
        conn,
        f"insert into {d}.sync_runs (source_id, trigger) values (%s, %s) returning id, started_at",
        (source_id, trigger),
    )
    assert started is not None, "an insert returning gives its row"
    await conn.execute(
        f"insert into {d}.source_cursors (source_id) values (%s) on conflict do nothing".encode(),
        (source_id,),
    )
    run_id: UUID = started["id"]
    deadline = time.monotonic() + budget if budget else None
    status, error, scan = "ok", None, None
    run: _Run | None = None
    source: Source | None = None
    try:
        source = build_source(row, env, deadline)
        source = await ready(conn, env, source)
        key = connector.scan_key(source)
        if key:
            scan = advisory_key(d, f"scan:{key}")
            if not await try_lock(conn, scan):
                scan = None
                raise _Busy
        run = _Run(conn, env, source, connector, started["started_at"], redactor)
        await run.permissions()
        await run.identities()
        await run.content()
        if run.c.partial:
            status = "partial"
        else:
            await run.finish()
    except _Busy:
        await conn.execute(f"delete from {d}.sync_runs where id = %s".encode(), (run_id,))
        return RunResult(None, "skipped", error="database busy")
    except Exception as e:  # a failed sync is recorded, not raised: the next run resumes
        status, error = "error", _error_text(e)
        log.warning("sync.failed", source=str(source_id), error=error)
    finally:
        if scan is not None:
            await unlock(conn, scan)
        if source is not None:
            with suppress(Exception):  # a connector's cleanup must not hide the run's result
                await connector.release(source)
    counts = run.c if run else _Counts()
    result = RunResult(run_id, status, counts.added, counts.updated, counts.deleted, error)
    await _finish(conn, env, row, started["started_at"], result)
    if run and not result.error and env.embed and env.data is not None:
        await embed_pending(conn, env.schemas, source_id, settings=env.data, meter=env.meter)
    return result


def _error_text(e: Exception) -> str:
    """What a failed run records and logs. A database error keeps its code and primary message
    only: its detail can quote row values."""
    if isinstance(e, psycopg.Error):
        return f"{type(e).__name__}: {e.sqlstate or ''} {e.diag.message_primary or ''}"[
            :ERROR_CHARS
        ]
    return f"{type(e).__name__}: {e}"[:ERROR_CHARS]


async def _finish(
    conn: AsyncConnection[DictRow], env: Env, row: DictRow, started: datetime, r: RunResult
) -> None:
    """Record the run and the source's standing, and the audit rows of a change of it."""
    d = env.schemas.data
    async with conn.transaction():
        await conn.execute(
            f"update {d}.sync_runs set status = %s, finished_at = now(), added = %s, updated = %s,"
            " deleted = %s, error = %s where id = %s".encode(),
            (r.status, r.added, r.updated, r.deleted, r.error, r.run_id),
        )
        await conn.execute(
            f"update {d}.source_cursors set last_status = %(st)s, last_error = %(err)s,"
            " last_sync_at = case when %(st)s = 'ok' then now() else last_sync_at end,"
            " errors = case when %(st)s = 'error' then errors + 1 else 0 end,"
            " requested_at = case when requested_at > %(at)s then requested_at end"
            " where source_id = %(src)s".encode(),
            {"st": r.status, "err": r.error, "at": started, "src": row["id"]},
        )
        failing_before = bool(row["errors"])
        for kind, when in (
            ("sync.failed", r.status == "error" and not failing_before),
            ("sync.recovered", r.status == "ok" and failing_before),
            ("sync.pruned", r.deleted > 0),
        ):
            if when:
                await audit.record_audit(
                    conn,
                    env.schemas,
                    tenant_id=row["tenant_id"],
                    kind=kind,
                    source_id=row["id"],
                    detail={"error": r.error} if r.error else {"deleted": r.deleted},
                )
