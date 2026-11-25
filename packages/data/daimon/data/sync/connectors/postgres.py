"""Rows of another Postgres database, read through a view its owner maintains.

The source's own view (and its own row-level security) decides what is visible; this
connector only reads it, through a role that cannot write, over a connection that passed the
network guard. One row is one record. Changes are read by keyset on the view's `updated_at`,
checkpointed page by page; a complete pass also lists every id and the engine deletes the
records that left the view. An empty list is distrusted by the engine.

Configuration (`sources.config`), credential `{"dsn": "postgresql://..."}`:

- `view`: the relation to read, `schema.name` or `name`.
- `columns`: record field to column; `id`, `title`, `body` and `updated_at` default to the
  same names; `occurred_at` (default `updated_at`), `url`, `author` and `container` are
  optional.
- `kind`: the kind of its records (default `row`). `page`: rows per read (default 500).
- `readers`: optional `{"column": ..., "workspace": ..., "kind": "user"}`; the column holds
  the ids of who may read the row, and the records are read through those access lists. Without
  it the source's scope rules decide.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Final

import psycopg
from daimon.data.access import subject
from daimon.data.sync.connector import (
    Acl,
    AclRef,
    Checkpoint,
    Connector,
    Descriptor,
    Field,
    ItemRecord,
    Json,
    Record,
    Source,
    SyncState,
    cursor_at,
    dsn_key,
)
from daimon.data.sync.netguard import NotPublic, resolve
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row

PAGE: Final = 500
SNAP: Final = 5000
"""Ids per read of the list a complete pass keeps."""
STATEMENT_MS: Final = 300_000
_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_DEFAULTS: Final = {"id": "id", "title": "title", "body": "body", "updated_at": "updated_at"}
_OPTIONAL: Final = ("occurred_at", "url", "author", "container")


class SourceError(RuntimeError):
    """A failure in reading the external database, worded so that it never echoes its URL."""


@dataclass(frozen=True)
class Config:
    view: sql.Identifier
    columns: dict[str, str]
    kind: str
    page: int
    readers: dict[str, str] | None

    @classmethod
    def of(cls, config: Json) -> Config:
        view = str(config.get("view") or "")
        parts = view.split(".")
        if not 1 <= len(parts) <= 2 or not all(_IDENT.match(p) for p in parts):
            raise SourceError("config.view must be a relation name, `schema.name` or `name`")
        mapped: Json = config.get("columns") or {}
        columns = {**_DEFAULTS, **{str(k): str(v) for k, v in mapped.items()}}
        readers = config.get("readers")
        wanted = [*columns.values(), *([str(readers["column"])] if readers else [])]
        if not all(_IDENT.match(c) for c in wanted):
            raise SourceError("config.columns must be column names")
        page = int(config.get("page") or PAGE)
        if not 1 <= page <= 10_000:
            raise SourceError("config.page must be between 1 and 10000")
        return cls(
            sql.Identifier(*parts),
            columns,
            str(config.get("kind") or "row"),
            page,
            {k: str(v) for k, v in readers.items()} if readers else None,
        )

    def ident(self, field: str) -> sql.Identifier:
        return sql.Identifier(self.columns[field])

    def selected(self) -> sql.Composed:
        """The fields a page reads, under fixed names; an unmapped optional one reads as null."""
        one = [
            sql.SQL("{}::text as id").format(self.ident("id")),
            sql.SQL("{} as title").format(self.ident("title")),
            sql.SQL("{} as body").format(self.ident("body")),
            sql.SQL("coalesce({}, 'epoch'::timestamptz) as k").format(self.ident("updated_at")),
        ]
        for name in _OPTIONAL:
            one.append(
                sql.SQL("{} as {}").format(self.ident(name), sql.Identifier(name))
                if name in self.columns
                else sql.SQL("null as {}").format(sql.Identifier(name))
            )
        if self.readers:
            one.append(sql.SQL("{} as readers").format(sql.Identifier(self.readers["column"])))
        return sql.SQL(", ").join(one)

    def page_query(self) -> sql.Composed:
        key = sql.SQL("coalesce({}, 'epoch'::timestamptz)").format(self.ident("updated_at"))
        ident = sql.SQL("{}::text").format(self.ident("id"))
        return sql.SQL(
            "select {fields} from {view} where %(t)s::timestamptz is null"
            " or ({key}, {ident}) > (%(t)s, %(id)s::text) order by {key}, {ident} limit {page}"
        ).format(
            fields=self.selected(),
            view=self.view,
            key=key,
            ident=ident,
            page=sql.Literal(self.page),
        )

    def ids_query(self) -> sql.Composed:
        ident = sql.SQL("{}::text").format(self.ident("id"))
        return sql.SQL(
            "select {ident} as id from {view} where {ident} > %(after)s order by 1 limit {n}"
        ).format(ident=ident, view=self.view, n=sql.Literal(SNAP))


@contextmanager
def _errors(view: str, timeout_ms: int) -> Iterator[None]:
    try:
        yield
    except psycopg.errors.QueryCanceled:
        raise SourceError(f"the read of {view} timed out ({timeout_ms / 1000:g} s)") from None
    except psycopg.errors.InsufficientPrivilege:
        raise SourceError(f"this role cannot read {view}") from None
    except psycopg.errors.UndefinedTable:
        raise SourceError(f"there is no {view} here; check the database URL") from None
    except psycopg.errors.UndefinedColumn as e:
        raise SourceError(f"{view}: {e.diag.message_primary}") from None
    except psycopg.OperationalError:
        raise SourceError("cannot connect to the database") from None


class PostgresConnector(Connector):
    kind: ClassVar[str] = "postgres"
    name: ClassVar[str] = "External Postgres"
    category: ClassVar[str] = "knowledge"
    fields: ClassVar[tuple[Field, ...]] = (
        Field("dsn", "Reader database URL", "secret", placeholder="postgresql://reader:…@host/db"),
        Field("view", "View to read", placeholder="public.documents"),
    )

    def descriptor_for(self, source: Source) -> Descriptor | None:
        return Descriptor(source.provider) if source.config.get("readers") else None

    def scan_key(self, source: Source) -> str | None:
        return dsn_key(source.credentials.get("dsn"))

    async def _connect(self, source: Source) -> AsyncConnection[DictRow]:
        dsn = source.credentials.get("dsn") or ""
        try:
            parts = conninfo_to_dict(dsn)
        except psycopg.ProgrammingError:
            raise SourceError("the database URL is not a valid Postgres URL") from None
        host = str(parts.get("host") or "")
        if not host or "," in host or host.startswith("/"):
            raise SourceError("the database URL needs one host name or address")
        try:
            ip = await resolve(host, int(parts.get("port") or 5432), source.private_hosts)
        except NotPublic as e:
            raise SourceError(str(e)) from None
        with _errors("the database", 10_000):
            conn = await AsyncConnection[DictRow].connect(
                dsn,
                hostaddr=ip,
                connect_timeout=10,
                autocommit=True,
                row_factory=dict_row,
                prepare_threshold=None,
                application_name="daimon-data-sync",
                options="-c default_transaction_read_only=on",
            )
        try:
            row = await (
                await conn.execute(
                    "select rolsuper or rolbypassrls as bad from pg_roles"
                    " where rolname = current_user"
                )
            ).fetchone()
        except BaseException:
            await conn.close()
            raise
        if row and row["bad"]:
            await conn.close()
            raise SourceError("this role can bypass row security; use a read-only role")
        return conn

    async def _read(
        self, conn: AsyncConnection[DictRow], cfg: Config, query: sql.Composed, args: Json
    ) -> list[DictRow]:
        """One read-only transaction with a statement timeout."""
        with _errors(cfg.view.as_string(), STATEMENT_MS):
            async with conn.transaction():
                await conn.execute("set transaction read only")
                await conn.execute(
                    "select set_config('statement_timeout', %s, true)", (str(STATEMENT_MS),)
                )
                return await (await conn.execute(query, args)).fetchall()

    def record(self, cfg: Config, source: Source, r: DictRow) -> ItemRecord | None:
        title, body = str(r["title"] or "").strip(), str(r["body"] or "").strip()
        if not (title or body):
            return None  # nothing yet: it is read again when the row changes
        at: datetime = r["occurred_at"] or r["k"]
        container = str(r["container"]) if r["container"] else None
        return ItemRecord(
            external_id=f"{cfg.kind}:{r['id']}",
            kind=cfg.kind,
            title=title,
            body=body,
            occurred_at=at if at.tzinfo else at.replace(tzinfo=UTC),
            container=container,
            container_kind="collection",
            author=str(r["author"]) if r["author"] else None,
            url=str(r["url"]) if r["url"] else None,
            payload={"id": r["id"]},
            facets={"container": container} if container else {},
            acl=self._acl(cfg, source, r) if cfg.readers else None,
        )

    @staticmethod
    def _acl(cfg: Config, source: Source, r: DictRow) -> Acl:
        assert cfg.readers is not None
        ids = sorted({str(x) for x in r["readers"] or ()})
        workspace, kind = cfg.readers.get("workspace", "default"), cfg.readers.get("kind", "user")
        return Acl(tuple(AclRef(subject(source.provider, workspace, kind, i)) for i in ids))

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config.of(source.config)
        state: Json = {"cursor": source.state.get("cursor")}
        conn = await self._connect(source)
        try:
            at = cursor_at(state["cursor"])
            query = cfg.page_query()
            while True:
                await source.budget.take()
                rows = await self._read(conn, cfg, query, {"t": at and at[0], "id": at and at[1]})
                for r in rows:
                    if rec := self.record(cfg, source, r):
                        yield rec
                if rows:
                    at = (rows[-1]["k"], rows[-1]["id"])
                    state["cursor"] = f"{at[0].isoformat()}|{at[1]}"
                    yield Checkpoint(dict(state))
                if len(rows) < cfg.page:
                    break
            live: set[str] = set()
            after = ""
            while True:
                await source.budget.take()
                ids = await self._read(conn, cfg, cfg.ids_query(), {"after": after})
                live |= {f"{cfg.kind}:{r['id']}" for r in ids}
                if len(ids) < SNAP:
                    break
                after = ids[-1]["id"]
            yield SyncState(state, live)
        finally:
            await conn.close()

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            cfg = Config.of(source.config)
            conn = await self._connect(source)
        except SourceError as e:
            return False, str(e)
        try:
            query = sql.SQL("select count(*) as n from {}").format(cfg.view)
            [row] = await self._read(conn, cfg, query, {})
        except SourceError as e:
            return False, str(e)
        finally:
            await conn.close()
        return True, f"Connected · {row['n']} rows visible"
