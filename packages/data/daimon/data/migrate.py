"""Applying the store's SQL files, from an Alembic revision or `daimon data init`.

The files are plain SQL scripts run as one multi-statement script each, so they can use
`DO` blocks and `{{data}}`/`{{views}}` placeholders. Each applied file is recorded in
`<data>.schema_version`, so a file runs once per store. The setup in `preamble.sql` and the
files in `GRANT_FILES` are idempotent and run every time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import structlog
from daimon.data.locks import CLUSTER, advisory_key
from daimon.data.schema import Schemas, read_sql, render

log = structlog.get_logger()

ROLES: Final = (
    "daimon_data_reader",
    "daimon_data_sql",
    "daimon_data_view_owner",
    "daimon_data_parity",
)

STORE_FILES: Final[tuple[str, ...]] = (
    "0001_store.sql",
    "0002_sync.sql",
    "0003_store.sql",
    "0004_identity.sql",
    "0005_ops.sql",
)
"""Schema files `daimon data init` applies, in order. An Alembic revision pins its own copy of
the names it applied, so this list may grow without them."""

GRANT_FILES: Final[dict[str, str]] = {
    "0001_store.sql": "0001_grants.sql",
    "0002_sync.sql": "0002_grants.sql",
    "0003_store.sql": "0003_grants.sql",
    "0005_ops.sql": "0005_grants.sql",
}
"""A store file's idempotent follow-up (grants, and parts that wait for an optional extension),
re-run by every apply and by `--repair` once that store file is in."""

_ROLE_LIST: Final = ", ".join(f"'{r}'" for r in ROLES)
_PROBE: Final = f"""
select has_database_privilege(current_user, current_database(), 'CREATE'),
  coalesce((select rolsuper or rolcreaterole from pg_roles where rolname = current_user), false),
  (select count(*) from pg_roles where rolname in ({_ROLE_LIST})) = {len(ROLES)},
  (select coalesce(bool_and(pg_has_role(current_user, oid, 'MEMBER WITH ADMIN OPTION')
     or (rolname <> 'daimon_data_reader' and pg_has_role(current_user, oid, 'SET'))), true)
   from pg_roles where rolname in ({_ROLE_LIST})),
  not exists (select from pg_roles where rolname in ({_ROLE_LIST})
    and (rolsuper or rolbypassrls or rolcanlogin or rolcreaterole or rolcreatedb or rolreplication))
  and not exists (select from pg_auth_members m join pg_roles r on r.oid = m.member
    join pg_roles g on g.oid = m.roleid where r.rolname in ({_ROLE_LIST})
    and (r.rolname, g.rolname) <> ('daimon_data_view_owner', 'daimon_data_reader'))
"""
"""Whether this role may create the store, make or hold the roles (SET on each, ADMIN on the
reader, which the view owner is granted), and whether existing roles are safe: the names are
cluster-wide, so one made elsewhere must not log in, bypass row security or hold other roles."""


class Sql(Protocol):
    """The two things the runner needs from a connection, so two drivers can share it."""

    def query(self, sql: str) -> list[tuple[object, ...]]: ...

    def script(self, sql: str) -> None: ...


@dataclass(frozen=True)
class Facts:
    can_create: bool
    can_create_roles: bool
    roles_exist: bool
    roles_held: bool
    roles_safe: bool


@dataclass(frozen=True)
class Outcome:
    applied: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    """What the server lacks; when set, nothing was applied."""


def probe(sql: Sql) -> Facts:
    row = sql.query(_PROBE)[0]
    return Facts(*(bool(v) for v in row))


def missing_prerequisites(facts: Facts) -> tuple[str, ...]:
    """What stops the store being set up by this role, as short phrases."""
    out: list[str] = []
    if not facts.can_create:
        out.append("CREATE on the database")
    if not (facts.can_create_roles or facts.roles_exist):
        out.append("CREATEROLE, or the daimon_data_* roles created by an administrator")
    if not facts.roles_held:
        out.append("SET on the daimon_data_* roles and ADMIN on daimon_data_reader")
    if not facts.roles_safe:
        out.append(
            "daimon_data_* roles that cannot log in, bypass row security or hold other roles"
        )
    return tuple(out)


def apply(sql: Sql, schemas: Schemas, files: Sequence[str], *, repair: bool = False) -> Outcome:
    """Set up the store: missing prerequisites return `Outcome.missing`; nothing is half-applied.

    Takes one lock for every store on the server, since the roles are shared, so two starts,
    or a start and `init`, never interleave.
    """
    missing = missing_prerequisites(probe(sql))
    if missing:
        return Outcome(missing=missing)
    sql.query(f"select pg_advisory_xact_lock({advisory_key(CLUSTER, 'migrate')})")
    sql.script(render(read_sql("preamble.sql"), schemas))
    done = {str(r[0]) for r in sql.query(f"select file from {schemas.data}.schema_version")}
    applied: list[str] = []
    for name in files:
        if name in done:
            continue
        sql.script(render(read_sql(name), schemas))
        sql.script(f"insert into {schemas.data}.schema_version (file) values ('{name}')")
        applied.append(name)
    if repair:
        sql.script(render(read_sql("repair.sql"), schemas))
    for store_file, name in GRANT_FILES.items():
        if store_file in done or store_file in applied:
            sql.script(render(read_sql(name), schemas))
    return Outcome(applied=tuple(applied))
