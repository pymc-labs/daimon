"""The declared file: sources, scopes and grants a deployment owns (`DAIMON_DATA__DECLARED`).

A YAML or JSON file, applied at start and every 10 minutes:

    revision: 4
    tenants:
      - workspace: <platform>:<workspace id>
        scopes: [{id: shared:eng, kind: shared, name: Engineering}]
        grants: [{scope: shared:eng, holder: "account:<uuid>"}]
        sources:
          - {id: wiki, kind: postgres, name: Wiki, credentials: {dsn: "env:WIKI_DSN"},
             publish: true}

What it never does: delete. A removed entry is left as it was; a source is retired only by
`retire: true`, and a retired source never reopens. A `revision` below the last applied one
is ignored. Rows it writes are `bound`: read-only in the app, changed only here. A source is
created in shadow and published only with `publish: true` and a met gate (`parity.gate`),
after retiring the other sources it `replaces` (by declared id or name). `publish: false`
returns a published source to shadow. Credentials are references, `env:NAME` or
`file:/path`, never values; one that cannot be read pauses a new source. A source's
`auto_link_domain` joins its config. A `workspace` is a tenant id, or `<platform>:<workspace
id>` resolved to the tenant here, so one file serves every deployment; an unknown one is skipped.

`redaction_sets` entries are rule sets, inline or a repo file pinned by sha
(`daimon.data.redaction`); a source names one in `redaction_set`.

`connections` entries are shared MCP connections (`daimon.core.shared_connections`): `id` is
the name, with `url`, a `token` reference and a `scope`. The web app keeps their address, token
and scope read-only; admins still read and approve their tools there.

Sections are handlers registered by name (`register_section`), so other features add theirs;
a section nobody handles is logged and kept in `declared_state.unknown`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

import structlog
import yaml
from cryptography.fernet import InvalidToken
from daimon.core.config import Settings
from daimon.core.ports import mcp_connections as shared
from daimon.data import audit, parity, pool, redaction
from daimon.data.access import ACL_SCOPE
from daimon.data.locks import advisory_key
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS
from daimon.data.sync.credentials import resolve_ref, seal
from daimon.data.sync.engine import Env
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

log = structlog.get_logger()

EVERY: Final = 600.0
"""Seconds between applies."""
KEY: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
GUC: Final = "daimon_data.declared"


class DeclaredError(Exception):
    """The declaration cannot be used. The message holds no declared value."""


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=KEY)
    kind: str
    name: str = Field(min_length=1)
    config: dict[str, object] = {}
    credentials: dict[str, str] = {}
    default_scope: str | None = None
    provider: str | None = None
    mode: Literal["stored", "federated"] = "stored"
    fresh_floor_minutes: int = Field(default=60, ge=0)
    fresh_ceiling_minutes: int | None = Field(default=None, gt=0)
    auto_link_domain: str | None = None
    replaces: list[str] = []
    redaction_set: str | None = Field(default=None, pattern=KEY)
    """The rule set its records are redacted by as they are ingested (`redaction_sets`)."""
    publish: bool = False
    retire: bool = False


class ScopeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    kind: Literal["public", "internal", "shared", "client", "restricted"]
    name: str = Field(min_length=1)
    description: str = ""
    release: bool = False
    """Hand the row back to admins; it stays."""


class GrantEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1)
    holder: str = Field(min_length=1)
    release: bool = False


class RedactionSetEntry(BaseModel):
    """A rule set: inline `rules`, or a repo file (`repo`, `path`, `ref`) pinned by blob sha.
    `sha` approves that sha, each time it changes; later shas wait for a data admin."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=KEY)
    rules: redaction.Rules | None = None
    repo: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    path: str | None = Field(default=None, min_length=1)
    ref: str | None = None
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    token: str | None = Field(default=None, pattern=r"^(env|file):.+$")
    """A reference to the repo host's token, never the token."""

    @model_validator(mode="after")
    def _one_kind(self) -> RedactionSetEntry:
        if (self.rules is None) == (self.repo is None) or (self.repo is None) != (
            self.path is None
        ):
            raise ValueError("a set has either rules, or a repo and a path")
        if self.rules is not None and (self.ref or self.sha or self.token):
            raise ValueError("ref, sha and token belong to a repo file")
        return self


class TenantDecl(BaseModel):
    """One workspace's sections. Sections nobody handles are kept as extra fields."""

    model_config = ConfigDict(extra="allow")
    workspace: uuid.UUID | Annotated[str, Field(pattern=r"^[a-z]+:\S+$")]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    tenants: list[TenantDecl] = []


def load(path: str) -> Declaration:
    """The declaration in `path`; `DeclaredError` if it is unreadable or invalid, naming
    locations and rules but never the input."""
    try:
        raw: object = yaml.safe_load(Path(path).read_text("utf-8"))  # pyright: ignore[reportUnknownMemberType]
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise DeclaredError(f"declaration unreadable ({type(e).__name__})") from None
    try:
        return Declaration.model_validate(raw)
    except ValidationError as e:
        where = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise DeclaredError(f"declaration invalid: {where}") from None


@dataclass
class Ctx:
    """What a section handler works with, inside the apply's transaction."""

    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant_id: uuid.UUID
    revision: int


type Result = dict[str, str]
type Handler = Callable[[Ctx, object], Awaitable[list[Result]]]
SECTIONS: dict[str, Handler] = {}


def register_section(name: str, handler: Handler) -> None:
    """Add a section a tenant entry may carry. One name, one handler."""
    if name in SECTIONS:
        raise ValueError(f"declared section {name} is already registered")
    SECTIONS[name] = handler


def _why(e: Exception) -> str:
    return str(e) if isinstance(e, DeclaredError) else type(e).__name__


async def _each[T: BaseModel](
    ctx: Ctx, section: str, raw: object, model: type[T], one: Callable[[Ctx, T], Awaitable[Result]]
) -> list[Result]:
    """Validate the section, then apply each entry in a savepoint: a failing entry leaves no
    part of itself and the others go on."""
    try:
        entries = TypeAdapter(list[model]).validate_python(raw)
    except ValidationError as e:
        where = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise DeclaredError(f"{section} invalid: {where}") from None
    out: list[Result] = []
    for entry in entries:
        key = str(getattr(entry, "id", None) or getattr(entry, "scope", ""))
        try:
            async with ctx.conn.transaction():
                out.append({"section": section, "id": key, **await one(ctx, entry)})
        except Exception as e:
            log.error("data.declared_entry_failed", section=section, id=key, why=_why(e))
            out.append({"section": section, "id": key, "action": "error", "error": _why(e)})
    return out


# ---- scopes and grants ---------------------------------------------------------------------


async def _scope(ctx: Ctx, e: ScopeEntry) -> Result:
    d = ctx.schemas.data
    cur = await ctx.conn.execute(
        f"select kind, name, description, bound from {d}.scopes"
        " where tenant_id = %s and id = %s".encode(),
        (ctx.tenant_id, e.id),
    )
    row = await cur.fetchone()
    if row is None:
        await ctx.conn.execute(
            f"insert into {d}.scopes (tenant_id, id, kind, name, description, bound)"
            " values (%s, %s, %s, %s, %s, %s)".encode(),
            (ctx.tenant_id, e.id, e.kind, e.name, e.description, not e.release),
        )
        return {"action": "create"}
    if not row["bound"] and not e.release:
        raise DeclaredError("a scope with this id exists and is not managed")
    same = (row["kind"], row["name"], row["description"]) == (e.kind, e.name, e.description)
    if same and row["bound"] != e.release:
        return {"action": "unchanged"}
    await ctx.conn.execute(
        f"update {d}.scopes set kind = %s, name = %s, description = %s, bound = %s"
        " where tenant_id = %s and id = %s".encode(),
        (e.kind, e.name, e.description, not e.release, ctx.tenant_id, e.id),
    )
    return {"action": "release" if e.release else "update"}


async def _grant(ctx: Ctx, e: GrantEntry) -> Result:
    d = ctx.schemas.data
    cur = await ctx.conn.execute(
        f"insert into {d}.grants as g (tenant_id, scope, holder, bound) values (%s, %s, %s, %s)"
        " on conflict (tenant_id, holder, scope) do update set bound = excluded.bound"
        " where g.bound is distinct from excluded.bound returning id".encode(),
        (ctx.tenant_id, e.scope, e.holder, not e.release),
    )
    if not await cur.fetchone():
        return {"action": "unchanged"}
    return {"action": "release" if e.release else "set"}


# ---- sources -------------------------------------------------------------------------------


def _resolve(ref: str) -> str | None:
    """A credential reference: `env:NAME` or `file:/path`. None: cannot be read."""
    try:
        return resolve_ref(ref)
    except ValueError as e:
        raise DeclaredError(str(e)) from None


def _prints(values: dict[str, str]) -> dict[str, str]:
    return {k: hashlib.sha256(v.encode()).hexdigest()[:12] for k, v in values.items()}


async def _twins(ctx: Ctx, e: SourceEntry) -> list[uuid.UUID]:
    """The sources `e` replaces, each named by declared id or name and matching exactly one."""
    out: list[uuid.UUID] = []
    for ref in e.replaces:
        cur = await ctx.conn.execute(
            f"select id from {ctx.schemas.data}.sources where tenant_id = %s"
            " and publication <> 'retired'"
            " and coalesce(declared_key, '') <> %s and (declared_key = %s or name = %s)".encode(),
            (ctx.tenant_id, e.id, ref, ref),
        )
        found = await cur.fetchall()
        if len(found) != 1:
            raise DeclaredError(f"replaces: {len(found)} sources match one entry")
        out.append(found[0]["id"])
    return out


async def _retire(ctx: Ctx, source_id: uuid.UUID, kind: str, **detail: object) -> None:
    await ctx.conn.execute(
        f"update {ctx.schemas.data}.sources set status = 'disabled', publication = 'retired'"
        " where id = %s".encode(),
        (source_id,),
    )
    await audit.record_audit(
        ctx.conn, ctx.schemas, tenant_id=ctx.tenant_id, kind=kind, source_id=source_id,
        detail={k: str(v) for k, v in detail.items()},
    )  # fmt: skip


async def _publish_step(
    ctx: Ctx, e: SourceEntry, source_id: uuid.UUID, publication: str
) -> str | None:
    """The step toward what `publish` says: rollback, wait (gate not met), publish; or None."""
    if not e.publish:
        if publication == "published":
            await parity.unpublish(ctx.conn, ctx.schemas, source_id)
            return "rollback"
        return None
    if publication != "shadow":
        return None
    twins = await _twins(ctx, e)
    if not (await parity.publish(ctx.conn, ctx.schemas, source_id)).met:
        return "wait"
    for twin in twins:
        await _retire(ctx, twin, "source.retired", replaced_by=e.id)
    return "publish"


async def _source(ctx: Ctx, e: SourceEntry) -> Result:
    d = ctx.schemas.data
    connector = CONNECTORS.get(e.kind)
    if connector is None:
        raise DeclaredError("unknown source kind")
    if connector.live != (e.mode == "federated"):
        raise DeclaredError("the kind and the mode disagree about live reads")
    cur = await ctx.conn.execute(
        f"select s.id, s.kind, s.status, s.publication, s.bound, s.redaction_set,"
        f" b.credentials as bound_prints from {d}.sources s"
        f" left join {d}.declared_bindings b on b.source_id = s.id"
        " where s.tenant_id = %s and s.declared_key = %s".encode(),
        (ctx.tenant_id, e.id),
    )
    row = await cur.fetchone()
    if row and row["publication"] == "retired":
        return {"action": "retired"}
    if row and row["kind"] != e.kind:
        raise DeclaredError("the kind of a declared source cannot change")
    if row and e.retire:
        await _retire(ctx, row["id"], "source.retired", by="declaration")
        return {"action": "retire"}
    resolved = {k: _resolve(v) for k, v in e.credentials.items()}
    values = {k: v for k, v in resolved.items() if v is not None}
    unreadable = len(resolved) - len(values)
    prints = _prints(values)
    stored: dict[str, str] = (row["bound_prints"] if row else None) or {}
    sealed = None
    if e.credentials and not unreadable and prints != stored:
        sealed = seal(ctx.env.crypto_keys, values)
    config = {
        **e.config,
        **({"auto_link_domain": e.auto_link_domain} if e.auto_link_domain else {}),
    }
    scope = (
        e.default_scope
        or connector.default_scope
        or (ACL_SCOPE if connector.descriptor else "internal")
    )
    fields = (
        e.name,
        e.provider,
        Jsonb(config),
        scope,
        e.mode,
        e.fresh_floor_minutes,
        e.fresh_ceiling_minutes,
        e.redaction_set,
    )
    if row is None:
        cur = await ctx.conn.execute(
            f"insert into {d}.sources (tenant_id, kind, name, provider, config, default_scope,"
            " mode,"
            " fresh_floor_minutes, fresh_ceiling_minutes, redaction_set, credential, status, bound,"
            " declared_key) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)"
            " returning id".encode(),
            (ctx.tenant_id, e.kind, *fields, sealed, "paused" if unreadable else "active", e.id),
        )
        created = await cur.fetchone()
        assert created is not None, "an insert returns its row"
        source_id, action, publication = created["id"], "create", "shadow"
        await audit.record_audit(
            ctx.conn,
            ctx.schemas,
            tenant_id=ctx.tenant_id,
            kind="declared.source_created",
            source_id=source_id,
        )
    else:
        source_id, publication = row["id"], row["publication"]
        status = row["status"]
        lacked = any(k not in stored for k in e.credentials)
        if status == "active" and unreadable and lacked:
            status = "paused"
        elif status == "paused" and not unreadable and lacked:
            status = "active"
        cur = await ctx.conn.execute(
            f"update {d}.sources set name = %s::text, provider = %s::text, config = %s::jsonb,"
            " default_scope = %s::text, mode = %s::text, fresh_floor_minutes = %s::int,"
            " fresh_ceiling_minutes = %s::int, redaction_set = %s::text,"
            " credential = coalesce(%s::text, credential),"
            " status = %s::text where id = %s and ((name, provider, config, default_scope, mode,"
            " fresh_floor_minutes, fresh_ceiling_minutes, redaction_set, status) is distinct from"
            " (%s::text, %s::text, %s::jsonb, %s::text, %s::text, %s::int, %s::int, %s::text,"
            " %s::text)"
            " or %s::text is not null) returning id".encode(),
            (*fields, sealed, status, source_id, *fields, status, sealed),
        )
        action = "update" if await cur.fetchone() else "unchanged"
        if action == "update" and row["redaction_set"] != e.redaction_set:
            await ctx.conn.execute(
                f"update {d}.source_cursors set state = '{{}}', requested_at = now()"
                " where source_id = %s".encode(),
                (source_id,),
            )
    await ctx.conn.execute(
        f"insert into {d}.declared_bindings (source_id, revision, publish, replaces, credentials)"
        " values (%s, %s, %s, %s, %s) on conflict (source_id) do update set"
        " revision = excluded.revision, publish = excluded.publish,"
        " replaces = excluded.replaces, credentials = excluded.credentials".encode(),
        (source_id, ctx.revision, e.publish, e.replaces, Jsonb(stored if unreadable else prints)),
    )
    out: Result = {"action": action}
    if unreadable:
        out["unreadable"] = str(unreadable)
    if step := await _publish_step(ctx, e, source_id, publication):
        out["publish"] = step
    return out


async def _sources(ctx: Ctx, raw: object) -> list[Result]:
    return await _each(ctx, "sources", raw, SourceEntry, _source)


async def _scopes(ctx: Ctx, raw: object) -> list[Result]:
    return await _each(ctx, "scopes", raw, ScopeEntry, _scope)


async def _grants(ctx: Ctx, raw: object) -> list[Result]:
    return await _each(ctx, "grants", raw, GrantEntry, _grant)


# ---- redaction sets ------------------------------------------------------------------------


async def _redaction_set(ctx: Ctx, e: RedactionSetEntry) -> Result:
    d = ctx.schemas.data
    key = (ctx.tenant_id, e.id)
    cur = await ctx.conn.execute(
        f"select * from {d}.redaction_sets where tenant_id = %s and name = %s".encode(), key
    )
    row = await cur.fetchone()
    rules = Jsonb(e.rules.model_dump(mode="json")) if e.rules else None
    if row is None:
        await ctx.conn.execute(
            f"insert into {d}.redaction_sets (tenant_id, name, rules, repo, path, ref, token_ref,"
            " approved_sha, declared_sha) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)".encode(),
            (*key, rules, e.repo, e.path, e.ref, e.token, e.sha, e.sha),
        )
        return {"action": "create"}
    if e.rules is not None:
        same = row["rules"] == (e.rules.model_dump(mode="json")) and row["repo"] is None
        await ctx.conn.execute(
            f"update {d}.redaction_sets set rules = %s, repo = null, path = null, ref = null,"
            " token_ref = null, pin_sha = null, approved_sha = null, declared_sha = null,"
            " pending = null, error = null where tenant_id = %s and name = %s".encode(),
            (rules, *key),
        )
        if not same:
            await redaction.restart_sources(ctx.conn, ctx.schemas, *key)
        return {"action": "unchanged" if same else "update"}
    # A pinned file: the declared sha approves itself when it changes; else admins approve.
    fresh = row["repo"] is None
    new_sha = e.sha is not None and (fresh or e.sha != row["declared_sha"])
    approved = e.sha if new_sha else row["approved_sha"]
    use_pending = new_sha and row["pin_sha"] == e.sha and row["pending"] is not None
    await ctx.conn.execute(
        f"update {d}.redaction_sets set repo = %s, path = %s, ref = %s, token_ref = %s,"
        " approved_sha = %s, declared_sha = %s, rules = case when %s then pending"
        " when %s then null else rules end, pending = case when %s then null else pending end,"
        " pin_sha = case when %s then null else pin_sha end"
        " where tenant_id = %s and name = %s".encode(),
        (e.repo, e.path, e.ref, e.token, approved, e.sha or row["declared_sha"], use_pending,
         fresh, use_pending, fresh, *key),
    )  # fmt: skip
    if use_pending:
        await redaction.restart_sources(ctx.conn, ctx.schemas, *key)
    return {"action": "update" if new_sha or fresh else "unchanged"}


async def _redaction_sets(ctx: Ctx, raw: object) -> list[Result]:
    return await _each(ctx, "redaction_sets", raw, RedactionSetEntry, _redaction_set)


# ---- shared MCP connections ---------------------------------------------------------------


class ConnectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=f"^{shared.NAME_PATTERN.pattern}$")
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = Field(default=None, pattern=r"^(env|file):.+$")
    """A reference to the server's bearer token, never the token."""
    scope: str | None = Field(default=None, pattern=f"^{shared.SCOPE_PATTERN.pattern}$")


async def _connection(ctx: Ctx, e: ConnectionEntry) -> Result:
    try:
        url = shared.checked_address(e.url)
        token = None if e.token is None else _resolve(e.token)
        if e.token is not None and token is None:
            raise DeclaredError("the token cannot be read")
        if token is not None and not ctx.env.crypto_keys:
            raise DeclaredError("DAIMON_CRYPTO__KEYS is required to keep a token")
        fernet = shared.build_multifernet(ctx.env.crypto_keys) if token is not None else None
        sealed = None if fernet is None or token is None else shared.encrypt_auth(fernet, token)
    except shared.SharedConnectionError as err:
        raise DeclaredError(str(err)) from None
    cur = await ctx.conn.execute(
        b"select url, encrypted_auth, scope, declared from mcp_connections"
        b" where tenant_id = %s and name = %s",
        (ctx.tenant_id, e.id),
    )
    row = await cur.fetchone()
    if row is not None and not row["declared"]:
        raise DeclaredError("a connection with this name exists and is not declared")
    held = None
    if row is not None and row["encrypted_auth"] is not None and fernet is not None:
        try:
            held = shared.decrypt_token(fernet, row["encrypted_auth"])
        except InvalidToken:
            held = None
    if (
        row is not None
        and (row["url"], row["scope"], held) == (url, e.scope, token)
        and (token is not None or row["encrypted_auth"] is None)
    ):
        return {"action": "unchanged"}
    await ctx.conn.execute(
        b"insert into mcp_connections (tenant_id, name, url, encrypted_auth, scope, declared)"
        b" values (%s, %s, %s, %s, %s, true) on conflict (tenant_id, name) do update set"
        b" url = excluded.url, encrypted_auth = excluded.encrypted_auth,"
        b" scope = excluded.scope, updated_at = now()",
        (ctx.tenant_id, e.id, url, sealed, e.scope),
    )
    if row is not None and row["url"] != url:  # a new address: its tools wait for review
        await ctx.conn.execute(
            b"update mcp_connection_tools set enabled = false from mcp_connections c"
            b" where c.id = mcp_connection_tools.connection_id"
            b" and c.tenant_id = %s and c.name = %s",
            (ctx.tenant_id, e.id),
        )
    return {"action": "create" if row is None else "update"}


async def _connections(ctx: Ctx, raw: object) -> list[Result]:
    return await _each(ctx, "connections", raw, ConnectionEntry, _connection)


register_section("scopes", _scopes)
register_section("grants", _grants)
register_section("sources", _sources)
register_section("redaction_sets", _redaction_sets)
register_section("connections", _connections)


# ---- apply ---------------------------------------------------------------------------------


@dataclass
class Report:
    revision: int
    ignored: bool = False
    busy: bool = False
    results: list[Result] | None = None


async def _tenant(conn: AsyncConnection[DictRow], workspace: uuid.UUID | str) -> uuid.UUID | None:
    """The tenant a declared `workspace` names, by its id or its `<platform>:<id>`; None when
    no such tenant exists. The store's pool opens core's own database (`pool`), so core's
    `tenants` is on its search path."""
    if isinstance(workspace, uuid.UUID):
        cur = await conn.execute(b"select id from tenants where id = %s", (workspace,))
    else:
        platform, external_id = workspace.split(":", 1)
        cur = await conn.execute(
            b"select id from tenants where platform = %s and external_id = %s",
            (platform, external_id),
        )
    row = await cur.fetchone()
    return row["id"] if row else None


async def apply(
    conn: AsyncConnection[DictRow], schemas: Schemas, env: Env, decl: Declaration
) -> Report:
    """Apply `decl` in one transaction under an advisory lock; nothing is deleted. A revision
    below the applied one is ignored, a busy lock skips the apply (the next tick retries)."""
    results: list[Result] = []
    unknown: set[str] = set()
    async with conn.transaction():
        cur = await conn.execute(
            b"select pg_try_advisory_xact_lock(%s) as ok", (advisory_key(schemas.data, "declared"),)
        )
        got = await cur.fetchone()
        if not (got and got["ok"]):
            return Report(decl.revision, busy=True)
        cur = await conn.execute(f"select revision from {schemas.data}.declared_state".encode())
        state = await cur.fetchone()
        if state and decl.revision < state["revision"]:
            log.info("data.declared_ignored", revision=decl.revision, applied=state["revision"])
            return Report(decl.revision, ignored=True)
        await conn.execute(f"select set_config('{GUC}', 'on', true)".encode())
        for tenant in decl.tenants:
            tenant_id = await _tenant(conn, tenant.workspace)
            if tenant_id is None:
                log.warning("data.declared_workspace_unknown")
                results.append({"section": "workspace", "id": "", "action": "error",
                                "error": "no such workspace"})  # fmt: skip
                continue
            ctx = Ctx(conn, schemas, env, tenant_id, decl.revision)
            extra = tenant.model_extra or {}
            for name, handler in SECTIONS.items():
                if name in extra:
                    try:
                        async with conn.transaction():
                            results += await handler(ctx, extra[name])
                    except Exception as e:
                        log.error("data.declared_section_failed", section=name, why=_why(e))
                        results.append(
                            {"section": name, "id": "", "action": "error", "error": _why(e)}
                        )
            unknown |= set(extra) - set(SECTIONS)
        if unknown:
            log.warning("data.declared_unknown_sections", sections=sorted(unknown))
        await conn.execute(
            f"insert into {schemas.data}.declared_state (id, revision, applied_at, unknown)"
            " values (1, %s, now(), %s) on conflict (id) do update set"
            " revision = excluded.revision,"
            " applied_at = excluded.applied_at, unknown = excluded.unknown".encode(),
            (decl.revision, sorted(unknown)),
        )
    for r in results:
        if r.get("unreadable"):
            log.error("data.declared_credentials_unreadable", id=r["id"], count=r["unreadable"])
    return Report(decl.revision, results=results)


async def apply_from_settings(settings: Settings, env: Env) -> Report | None:
    """Apply the file `DAIMON_DATA__DECLARED` names; None when unset. Never raises."""
    if not settings.data.declared:
        return None
    try:
        decl = load(settings.data.declared)
        async with pool.connection(settings) as conn:
            return await apply(conn, env.schemas, env, decl)
    except Exception as e:
        log.error("data.declared_failed", why=_why(e))
        return None
