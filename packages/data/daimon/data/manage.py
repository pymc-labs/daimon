"""What a workspace's admins do to its sources, scopes and grants, and what they read of them.

The web app's writes. Every function takes the store owner's connection and the caller's
`tenant_id` and touches only that tenant's rows; each write leaves an audit row. A row the
declared file owns (`bound`) is never changed here. Errors are the identity ones (`Missing`,
`Conflict`), which carry a message safe to show, plus `Invalid` for input that does not fit.
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from daimon.data import parity
from daimon.data.access import ACL_SCOPE
from daimon.data.audit import record_audit
from daimon.data.identity import Conflict, IdentityError, Missing
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS
from daimon.data.sync.credentials import CredentialError, seal
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

MANAGED: Final = "The deployment manages this. Change it in the declared file."
HOLDER: Final = re.compile(r"^\S{1,200}$")
SCOPE_KINDS: Final = ("public", "internal", "shared", "client", "restricted")
Action = Literal["pause", "resume", "retire", "publish", "unpublish", "sync"]


importlib.import_module("daimon.data.sync.connectors")  # registers the kinds


class Invalid(IdentityError):
    """Input that does not fit; the message says what to fix."""


@dataclass(frozen=True)
class KindField:
    name: str
    label: str
    kind: str
    options: tuple[str, ...]
    required: bool
    placeholder: str


@dataclass(frozen=True)
class Kind:
    kind: str
    name: str
    category: str
    live: bool
    fields: tuple[KindField, ...]


@dataclass(frozen=True)
class ParityView:
    finished_at: datetime | None
    people: int
    checked: int
    wrong_more: int
    wrong_less: int
    unresolved: str | None
    ok: bool


@dataclass(frozen=True)
class SourceView:
    id: uuid.UUID
    name: str
    kind: str
    mode: str
    status: str
    publication: str
    bound: bool
    default_scope: str
    created_at: datetime
    records: int
    last_sync_at: datetime | None
    last_status: str | None
    last_error: str | None
    errors: int
    parity: ParityView | None
    can_publish: bool
    blocked: str | None
    """Why it cannot be published yet; None when it can (or is published)."""


@dataclass(frozen=True)
class RunView:
    id: uuid.UUID
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    added: int
    updated: int
    deleted: int
    error: str | None


@dataclass(frozen=True)
class ScopeView:
    id: str
    kind: str
    name: str
    description: str
    bound: bool
    holders: list[GrantView]


@dataclass(frozen=True)
class GrantView:
    holder: str
    bound: bool
    granted_by: uuid.UUID | None


@dataclass(frozen=True)
class IdentityView:
    provider: str
    workspace: str
    subject: str
    name: str
    email: str | None
    email_verified: bool
    account_id: uuid.UUID | None
    proof: str | None
    linked_at: datetime | None
    active: bool


@dataclass(frozen=True)
class AdminView:
    account_id: uuid.UUID
    granted_by: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True)
class PersonConnection:
    source_id: uuid.UUID
    source: str
    provider: str
    subject: str
    status: str
    last_ok_at: datetime | None
    made_link: bool


def kinds() -> list[Kind]:
    """The source kinds this release ships, with the form each one takes."""
    return sorted(
        (
            Kind(
                c.kind, c.name, c.category, c.live,
                tuple(
                    KindField(f.name, f.label, f.kind, f.options, f.required, f.placeholder)
                    for f in c.fields
                ),
            )
            for c in CONNECTORS.values()
        ),
        key=lambda k: k.name,
    )  # fmt: skip


async def _one(conn: AsyncConnection[DictRow], sql: str, args: Sequence[Any], what: str) -> DictRow:
    row = await (await conn.execute(sql.encode(), args)).fetchone()
    if row is None:
        raise Missing(what)
    return row


# ---- sources -------------------------------------------------------------------------------

_SOURCES = """
select s.id, s.name, s.kind, s.mode, s.status, s.publication, s.bound, s.default_scope,
  s.created_at, c.last_sync_at, c.last_status, c.last_error, coalesce(c.errors, 0) as errors,
  (select count(*) from {d}.records r where r.source_id = s.id) as records,
  p.finished_at, p.people, p.checked, p.wrong_more, p.wrong_less, p.unresolved, p.ok
from {d}.sources s
left join {d}.source_cursors c on c.source_id = s.id
left join lateral (select * from {d}.parity_runs x where x.source_id = s.id
  and x.generation = s.generation order by x.started_at desc, x.id desc limit 1) p on true
where s.tenant_id = %s {only} order by s.name, s.id
"""


async def sources(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID | None = None,
) -> list[SourceView]:
    """The workspace's sources with their sync state, newest parity run and publish gate."""
    only = "and s.id = %s" if source_id else ""
    args = (tenant_id, source_id) if source_id else (tenant_id,)
    cur = await conn.execute(_SOURCES.format(d=schemas.data, only=only).encode(), args)
    out: list[SourceView] = []
    for r in await cur.fetchall():
        run = (
            ParityView(r["finished_at"], r["people"], r["checked"], r["wrong_more"],
                       r["wrong_less"], r["unresolved"], r["ok"])
            if r["people"] is not None
            else None
        )  # fmt: skip
        gate = (
            await parity.gate(conn, schemas, r["id"])
            if r["publication"] == "shadow"
            else parity.Gate(r["publication"] == "published")
        )
        out.append(
            SourceView(
                r["id"],
                r["name"],
                r["kind"],
                r["mode"],
                r["status"],
                r["publication"],
                r["bound"],
                r["default_scope"],
                r["created_at"],
                r["records"],
                r["last_sync_at"],
                r["last_status"],
                r["last_error"],
                r["errors"],
                run,
                gate.met and r["publication"] == "shadow",
                gate.reason,
            )  # fmt: skip
        )
    return out


async def runs(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    limit: int = 20,
) -> list[RunView]:
    """A source's latest sync runs, newest first, with what each one failed on."""
    await _one(
        conn,
        f"select 1 from {schemas.data}.sources where id = %s and tenant_id = %s",
        (source_id, tenant_id),
        "That source isn't here.",
    )
    cur = await conn.execute(
        f"select id, trigger, status, started_at, finished_at, added, updated, deleted, error"
        f" from {schemas.data}.sync_runs where source_id = %s"
        " order by started_at desc limit %s".encode(),
        (source_id, limit),
    )
    return [RunView(**r) for r in await cur.fetchall()]


async def create_source(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    keys: Sequence[str],
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    kind: str,
    name: str,
    settings: dict[str, Any],
) -> uuid.UUID:
    """Add a source. `settings` holds the kind's form fields: secret ones are sealed as the
    credential, the rest are its config. It starts in shadow, unpublished."""
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise Invalid("Pick a kind of source.")
    if not name.strip():
        raise Invalid("Give the source a name.")
    known = {f.name for f in connector.fields}
    if extra := set(settings) - known:
        raise Invalid(f"Unknown setting: {sorted(extra)[0]}.")
    config: dict[str, Any] = {}
    credentials: dict[str, str] = {}
    for f in connector.fields:
        value = settings.get(f.name)
        if value in (None, "", []):
            if f.required:
                raise Invalid(f"{f.label} is required.")
            continue
        if f.kind == "select" and value not in f.options:
            raise Invalid(f"{f.label} is not one of the choices.")
        if f.kind == "secret":
            credentials[f.name] = str(value)
        else:
            config[f.name] = value
    try:
        sealed = seal(keys, credentials) if credentials else None
    except CredentialError:
        raise Invalid("Credentials can't be stored on this server.") from None
    scope = connector.default_scope or (ACL_SCOPE if connector.descriptor else "internal")
    cur = await conn.execute(
        f"insert into {schemas.data}.sources (tenant_id, kind, name, config, credential,"
        " default_scope, mode, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s)"
        " returning id".encode(),
        (tenant_id, kind, name.strip(), Jsonb(config), sealed, scope,
         "federated" if connector.live else "stored", by),
    )  # fmt: skip
    row = await cur.fetchone()
    assert row is not None, "an insert returns its row"
    await record_audit(
        conn, schemas, tenant_id=tenant_id, kind="source.created", account_id=by,
        source_id=row["id"], detail={"kind": kind},
    )  # fmt: skip
    return row["id"]


async def act(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    source_id: uuid.UUID,
    action: Action,
) -> None:
    """Pause, resume, retire, publish, unpublish or sync one source. Declared sources refuse,
    and a publish waits on the parity gate."""
    d = schemas.data
    async with conn.transaction():
        row = await _one(
            conn,
            f"select status, publication, bound from {d}.sources where id = %s and tenant_id = %s"
            " for update",
            (source_id, tenant_id),
            "That source isn't here.",
        )
        if row["bound"]:
            raise Conflict(MANAGED)
        if row["publication"] == "retired":
            raise Conflict("That source is retired.")
        sql = {
            "pause": "update {d}.sources set status = 'paused' where id = %s",
            "resume": "update {d}.sources set status = 'active' where id = %s",
            "retire": "update {d}.sources set status = 'disabled', publication = 'retired'"
            " where id = %s",
            "sync": "insert into {d}.source_cursors (source_id, requested_at) values (%s, now())"
            " on conflict (source_id) do update set requested_at = now()",
        }
        if action == "publish":
            gate = await parity.publish(conn, schemas, source_id, by)
            if not gate.met:
                raise Conflict(f"Not ready to publish: {gate.reason}.")
        elif action == "unpublish":
            await parity.unpublish(conn, schemas, source_id, by)
        else:
            await conn.execute(sql[action].format(d=d).encode(), (source_id,))
        if action not in ("publish", "unpublish", "sync"):
            await record_audit(
                conn, schemas, tenant_id=tenant_id, kind=f"source.{action}", account_id=by,
                source_id=source_id,
            )  # fmt: skip


# ---- scopes and grants -----------------------------------------------------------------------


async def scopes(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID
) -> list[ScopeView]:
    """The workspace's scopes, each with who holds it."""
    d = schemas.data
    cur = await conn.execute(
        f"select id, kind, name, description, bound from {d}.scopes where tenant_id = %s"
        " order by name, id".encode(),
        (tenant_id,),
    )
    found = await cur.fetchall()
    cur = await conn.execute(
        f"select scope, holder, bound, granted_by from {d}.grants where tenant_id = %s"
        " order by holder".encode(),
        (tenant_id,),
    )
    held: dict[str, list[GrantView]] = {}
    for g in await cur.fetchall():
        held.setdefault(g["scope"], []).append(GrantView(g["holder"], g["bound"], g["granted_by"]))
    return [ScopeView(r["id"], r["kind"], r["name"], r["description"], r["bound"],
                      held.get(r["id"], [])) for r in found]  # fmt: skip


async def create_scope(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    id: str,
    kind: str,
    name: str,
    description: str = "",
) -> None:
    if kind not in SCOPE_KINDS:
        raise Invalid("Pick a kind of scope.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,63}", id) or id == ACL_SCOPE:
        raise Invalid("A scope id uses lowercase letters, digits, dashes and colons.")
    if not name.strip():
        raise Invalid("Give the scope a name.")
    async with conn.transaction():
        cur = await conn.execute(
            f"insert into {schemas.data}.scopes (tenant_id, id, kind, name, description)"
            " values (%s, %s, %s, %s, %s) on conflict do nothing returning id".encode(),
            (tenant_id, id, kind, name.strip(), description.strip()),
        )
        if await cur.fetchone() is None:
            raise Conflict("A scope with that id exists.")
        await record_audit(
            conn, schemas, tenant_id=tenant_id, kind="scope.created", account_id=by, target=id
        )


async def delete_scope(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    id: str,
) -> None:
    """Remove a scope and its grants; refused while it is the default of a source."""
    d = schemas.data
    async with conn.transaction():
        row = await _one(
            conn,
            f"select bound from {d}.scopes where tenant_id = %s and id = %s for update",
            (tenant_id, id),
            "That scope isn't here.",
        )
        if row["bound"]:
            raise Conflict(MANAGED)
        cur = await conn.execute(
            f"select 1 from {d}.sources where tenant_id = %s"
            " and default_scope = %s limit 1".encode(),
            (tenant_id, id),
        )
        if await cur.fetchone():
            raise Conflict("A source still uses this scope.")
        await conn.execute(
            f"delete from {d}.scopes where tenant_id = %s and id = %s".encode(), (tenant_id, id)
        )
        await record_audit(
            conn, schemas, tenant_id=tenant_id, kind="scope.deleted", account_id=by, target=id
        )


async def grant(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    scope: str,
    holder: str,
) -> None:
    if not HOLDER.match(holder):
        raise Invalid("Name who holds the scope.")
    async with conn.transaction():
        await _one(
            conn,
            f"select 1 from {schemas.data}.scopes where tenant_id = %s and id = %s",
            (tenant_id, scope),
            "That scope isn't here.",
        )
        cur = await conn.execute(
            f"insert into {schemas.data}.grants (tenant_id, scope, holder, granted_by)"
            " values (%s, %s, %s, %s) on conflict do nothing returning id".encode(),
            (tenant_id, scope, holder, by),
        )
        if await cur.fetchone() is None:
            return
        await record_audit(
            conn, schemas, tenant_id=tenant_id, kind="grant.created", account_id=by,
            target=holder, detail={"scope": scope},
        )  # fmt: skip


async def revoke(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    scope: str,
    holder: str,
) -> None:
    d = schemas.data
    async with conn.transaction():
        row = await _one(
            conn,
            f"select bound from {d}.grants where tenant_id = %s and scope = %s and holder = %s"
            " for update",
            (tenant_id, scope, holder),
            "That grant isn't here.",
        )
        if row["bound"]:
            raise Conflict(MANAGED)
        await conn.execute(
            f"delete from {d}.grants where tenant_id = %s and scope = %s and holder = %s".encode(),
            (tenant_id, scope, holder),
        )
        await record_audit(
            conn, schemas, tenant_id=tenant_id, kind="grant.revoked", account_id=by,
            target=holder, detail={"scope": scope},
        )  # fmt: skip


# ---- people ----------------------------------------------------------------------------------


async def identities(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    linked: bool | None = None,
    limit: int = 200,
) -> list[IdentityView]:
    """Source identities of the workspace: one person's (`account_id`), or the linked or
    unlinked ones."""
    cur = await conn.execute(
        f"select provider, workspace, subject, name, email, email_verified, account_id, proof,"
        f" linked_at, active from {schemas.data}.identities where tenant_id = %(t)s"
        " and (%(a)s::uuid is null or account_id = %(a)s)"
        " and (%(l)s::boolean is null or (account_id is not null) = %(l)s)"
        " order by provider, workspace, name, subject limit %(n)s".encode(),
        {"t": tenant_id, "a": account_id, "l": linked, "n": limit},
    )
    return [IdentityView(**r) for r in await cur.fetchall()]


async def data_admins(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID
) -> list[AdminView]:
    cur = await conn.execute(
        f"select account_id, granted_by, created_at from {schemas.data}.data_admins"
        " where tenant_id = %s order by created_at".encode(),
        (tenant_id,),
    )
    return [AdminView(**r) for r in await cur.fetchall()]


async def person_connections(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[PersonConnection]:
    """A person's own sign-ins at their workspace's sources; never a token."""
    cur = await conn.execute(
        f"select c.source_id, s.name as source, c.provider, c.subject, c.status, c.last_ok_at,"
        f" c.made_link from {schemas.data}.person_connections c join {schemas.data}.sources s"
        " on s.id = c.source_id where c.tenant_id = %s and c.account_id = %s"
        " order by s.name".encode(),
        (tenant_id, account_id),
    )
    return [PersonConnection(**r) for r in await cur.fetchall()]


async def connectable(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    providers: Sequence[str],
) -> list[tuple[uuid.UUID, str, str]]:
    """The workspace's open sources a person may sign in to: (id, name, provider)."""
    cur = await conn.execute(
        f"select id, name, coalesce(provider, kind) as provider from {schemas.data}.sources"
        " where tenant_id = %s and status <> 'disabled' and publication <> 'retired'"
        " and coalesce(provider, kind) = any(%s) order by name".encode(),
        (tenant_id, list(providers)),
    )
    return [(r["id"], r["name"], r["provider"]) for r in await cur.fetchall()]
