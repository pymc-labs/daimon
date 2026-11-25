from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.data.init import PsycopgSql
from daimon.data.migrate import ROLES, apply, probe
from daimon.testing.data_db import DataDb

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
        assert _one(conn, f"select count(*) from {data_db.schemas.data}.schema_version") == 1


def test_apply_without_prerequisites_does_nothing(data_db: DataDb) -> None:
    name = f"nocreate_{uuid.uuid4().hex[:8]}"
    with data_db.connect() as conn:
        conn.execute(f"create role {name}")
        try:
            conn.execute(f"set role {name}")
            assert probe(PsycopgSql(conn)).can_create is False
            outcome = apply(PsycopgSql(conn), data_db.schemas, ())
            conn.execute("reset role")
        finally:
            conn.execute("reset role")
            conn.execute(f"drop role {name}")
    assert outcome.missing
    assert outcome.applied == ()


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
