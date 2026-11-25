from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.core.config import DataSettings
from daimon.data.init import PsycopgSql, init_data
from daimon.data.migrate import ROLES, STORE_FILES, apply
from daimon.data.schema import Schemas, read_sql, render
from daimon.testing.data_db import DataDb
from sqlalchemy import make_url

_FILE = "0000_probe.sql"


@pytest.fixture
def probe_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-table file the runner can record, standing in for the real schema files."""
    import daimon.data.migrate as migrate

    real = migrate.read_sql
    monkeypatch.setattr(
        migrate,
        "read_sql",
        lambda name: "create table {{data}}.probe (id int);" if name == _FILE else real(name),
    )


def _one(conn: Any, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


@pytest.mark.usefixtures("probe_file")
def test_apply_creates_schemas_and_roles_and_runs_each_file_once(data_db: DataDb) -> None:
    with data_db.connect() as conn:
        sql = PsycopgSql(conn)
        first = apply(sql, data_db.schemas, (_FILE,))
        second = apply(sql, data_db.schemas, (_FILE,))
        assert (first.applied, second.applied) == ((_FILE,), ())
        for schema in (data_db.schemas.data, data_db.schemas.views):
            assert _one(conn, f"select to_regnamespace('{schema}') is not null") is True
        roles = _one(conn, "select count(*) from pg_roles where rolname like 'daimon_data_%'")
        assert roles == len(ROLES)
        assert (
            _one(conn, f"select count(*) from {data_db.schemas.data}.schema_version")
            == len(STORE_FILES) + 1
        )


@pytest.mark.parametrize(
    ("grants", "missing"),
    [
        ("", "CREATE on the database"),
        ("createrole", "SET on the daimon_data_* roles"),  # roles it did not create
    ],
)
def test_apply_without_prerequisites_does_nothing(
    data_db: DataDb, grants: str, missing: str
) -> None:
    name = f"nopre_{uuid.uuid4().hex[:8]}"
    with data_db.connect() as conn:
        conn.execute(f"create role {name} {grants}")
        if grants:
            conn.execute(
                f"grant create on database {_one(conn, 'select current_database()')} to {name}"
            )
        try:
            conn.execute(f"set role {name}")
            outcome = apply(PsycopgSql(conn), data_db.schemas, ())
        finally:
            conn.execute("reset role")
            conn.execute(f"drop owned by {name}")
            conn.execute(f"drop role {name}")
    assert any(m.startswith(missing) for m in outcome.missing), outcome.missing
    assert outcome.applied == ()


def test_init_with_an_admin_url_leaves_the_store_to_the_app_user(data_db: DataDb) -> None:
    suffix = uuid.uuid4().hex[:8]
    name = f"app_{suffix}"
    data = DataSettings(schema_name=f"{data_db.schemas.data}_{suffix}")
    schemas = Schemas.of(data)
    url = make_url(data_db.url).set(username=name, password=name)
    with data_db.connect() as conn:
        conn.execute(f"create role {name} login password '{name}'")
    try:
        outcome = init_data(url.render_as_string(hide_password=False), data, admin_url=data_db.url)
        assert outcome.applied == STORE_FILES
        with data_db.connect() as conn:
            owners = conn.execute(
                "select distinct pg_get_userbyid(relowner) from pg_class"
                " where relnamespace = %s::regnamespace and relkind = 'r'",
                (schemas.data,),
            ).fetchall()
        assert owners == [(name,)]
    finally:
        with data_db.connect() as conn:
            conn.execute(f"drop schema if exists {schemas.views}, {schemas.data} cascade")
            conn.execute(f"drop owned by {name}")
            conn.execute(f"drop role {name}")


def test_repair_puts_view_ownership_back(data_db: DataDb) -> None:
    view = f"{data_db.schemas.views}.v"
    with data_db.connect() as conn:
        conn.execute(f"create or replace view {view} as select 1 as one")
        assert (
            _one(
                conn,
                f"select pg_get_userbyid(relowner) from pg_class where oid = '{view}'::regclass",
            )
            != "daimon_data_view_owner"
        )
        apply(PsycopgSql(conn), data_db.schemas, (), repair=True)
        owner = _one(
            conn, f"select pg_get_userbyid(relowner) from pg_class where oid = '{view}'::regclass"
        )
        assert owner == "daimon_data_view_owner"


def test_reapplying_the_store_is_a_no_op_and_puts_back_grants_a_no_privileges_restore_drops(
    data_db: DataDb,
) -> None:
    d = data_db.schemas.data
    reader = "'daimon_data_reader'"
    with data_db.connect() as conn:
        conn.execute(  # what a restore with --no-privileges leaves: default grants only
            f"grant execute on all functions in schema {d} to public;"
            f" revoke all on all tables in schema {d} from daimon_data_reader"
        )
        assert _one(conn, f"select has_table_privilege({reader}, '{d}.records', 'select')") is False
        outcome = apply(PsycopgSql(conn), data_db.schemas, STORE_FILES, repair=True)
        assert outcome.applied == (), "an applied file ran again"
        owner_only = _one(
            conn,
            f"select count(*) from pg_proc where pronamespace = '{d}'::regnamespace and not prosecdef"
            f" and has_function_privilege({reader}, oid, 'execute')",
        )
        assert owner_only == 0, "the reader still runs an owner's function"
        assert _one(conn, f"select has_table_privilege({reader}, '{d}.records', 'select')") is True
        assert _one(
            conn, f"select has_function_privilege({reader}, '{d}.visible_sources()', 'execute')"
        )
        assert (
            _one(
                conn,
                f"select has_column_privilege({reader}, '{d}.sources', 'credential', 'select')",
            )
            is False
        ), "the reader reads credentials"
        policies = _one(conn, f"select count(*) from pg_policies where schemaname = '{d}'")
        vectors = _one(conn, f"select to_regclass('{d}.chunk_vectors') is not null")
        assert policies == 6 + int(vectors), (
            "a policy went missing"
        )  # sources, records, chunks, attachments, datasets, metrics


def _shape(conn: Any, schemas: Schemas) -> set[tuple[str, ...]]:
    """The store's columns, functions, triggers, constraints, indexes, policies and files,
    with its schema names written as placeholders."""
    names = [schemas.data, schemas.views]
    rows = conn.execute(
        "select 'column', table_schema, table_name || '.' || column_name || ' ' || data_type"
        " from information_schema.columns where table_schema = any(%(s)s)"
        " union all select 'function', n.nspname, pg_get_functiondef(p.oid)"
        " from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = any(%(s)s)"
        " union all select 'trigger', n.nspname, c.relname || ' ' || pg_get_triggerdef(t.oid)"
        " from pg_trigger t join pg_class c on c.oid = t.tgrelid"
        " join pg_namespace n on n.oid = c.relnamespace where n.nspname = any(%(s)s)"
        " and not t.tgisinternal"
        " union all select 'constraint', n.nspname, conname || ' ' || pg_get_constraintdef(o.oid)"
        " from pg_constraint o join pg_namespace n on n.oid = o.connamespace"
        " where n.nspname = any(%(s)s)"
        " union all select 'index', schemaname, indexdef from pg_indexes"
        " where schemaname = any(%(s)s)"
        " union all select 'policy', schemaname, tablename || ' ' || policyname || ' ' || qual"
        " from pg_policies where schemaname = any(%(s)s)",
        {"s": names},
    ).fetchall()
    files = conn.execute(f"select file from {schemas.data}.schema_version").fetchall()
    normal = {(kind, *(t.replace(schemas.views, "{v}").replace(schemas.data, "{d}") for t in rest))
              for kind, *rest in rows}  # fmt: skip
    return normal | {("file", f) for (f,) in files}


def test_the_down_files_return_the_store_to_its_0003_shape(data_db: DataDb) -> None:
    """0004-0006 undone, newest first, leave what 0001-0003 alone make."""
    full, base = (
        Schemas.of(DataSettings(schema_name=f"{data_db.schemas.data}_{uuid.uuid4().hex[:8]}"))
        for _ in range(2)
    )
    later = STORE_FILES[3:]
    with data_db.connect() as conn:
        sql = PsycopgSql(conn)
        try:
            apply(sql, full, STORE_FILES)
            apply(sql, base, STORE_FILES[:3])
            assert _shape(conn, full) != _shape(conn, base), "the later files change nothing"
            for file in reversed(later):
                sql.script(render(read_sql(file.removesuffix(".sql") + "_down.sql"), full))
            assert _shape(conn, full) == _shape(conn, base)
        finally:
            for s in (full, base):
                conn.execute(f"drop schema if exists {s.views}, {s.data} cascade")
