"""Additive schema metadata cannot change the legacy behavior oracle."""

from __future__ import annotations

import importlib.util
import json
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from daimon.testing import effect_recorder
from daimon.testing.effect_recorder import (
    DATABASE_EXTENSIONS,
    DB_TABLES,
    LEGACY_DB_COLUMNS,
    EffectRecorder,
)
from daimon.testing.ma_transport import Json
from sqlalchemy import Column, Integer, MetaData, String, Table, Uuid, insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession

SPEC = importlib.util.spec_from_file_location(
    "projection_oracle_runner", Path(__file__).resolve().parents[1] / "golden/runner.py"
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


async def test_additive_columns_are_retained_without_changing_legacy_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real PostgreSQL queries against isolated scratch tables, with the exact
    # pinned column names. No domain ORM/schema changes are needed to simulate N2.
    metadata = MetaData()
    for name in DB_TABLES:
        Table(
            name,
            metadata,
            *(Column(column, String) for column in LEGACY_DB_COLUMNS[name]),
            Column[UUID]("binding_id", Uuid),
            Column("binding_generation", Integer),
            Column("continuation_id", String),
        )
    schema = f"projection_{secrets.token_hex(8)}"
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execution_options(schema_translate_map={None: schema})
    await connection.run_sync(metadata.create_all)
    monkeypatch.setattr(effect_recorder, "database_metadata", lambda: metadata)
    first_binding = uuid4()
    for table in metadata.tables.values():
        await db_session.execute(
            insert(table),
            [
                {
                    "id": str(uuid4()),
                    "tenant_id": "tenant_b",
                    "binding_id": None,
                    "binding_generation": 1,
                    "continuation_id": "new_runtime_b",
                },
                {
                    "id": str(uuid4()),
                    "tenant_id": "tenant_a",
                    "binding_id": first_binding,
                    "binding_generation": 9,
                    "continuation_id": "new_runtime_a",
                },
            ],
        )
    recorder = EffectRecorder()
    snapshot = await recorder.database(db_session)
    raw = json.loads(recorder.transcript(database=snapshot))
    extensions = raw["database_extensions"]
    assert DATABASE_EXTENSIONS not in raw["database"]
    for name in DB_TABLES:
        rows = snapshot[name]
        assert isinstance(rows, list)
        assert [row["tenant_id"] for row in rows if isinstance(row, dict)] == [
            "tenant_a",
            "tenant_b",
        ]
        assert all(
            isinstance(row, dict) and set(row) == set(LEGACY_DB_COLUMNS[name]) for row in rows
        )
        assert extensions[name] == {
            "columns": ["binding_id", "binding_generation", "continuation_id"],
            "rows": [
                {
                    "legacy_row_index": 0,
                    "values": {
                        "binding_id": str(first_binding),
                        "binding_generation": 9,
                        "continuation_id": "new_runtime_a",
                    },
                },
                {
                    "legacy_row_index": 1,
                    "values": {
                        "binding_id": None,
                        "binding_generation": 1,
                        "continuation_id": "new_runtime_b",
                    },
                },
            ],
        }
    legacy = recorder.transcript(
        database={key: value for key, value in snapshot.items() if key != DATABASE_EXTENSIONS}
    )
    assert RUNNER.legacy_transcript(recorder.transcript(database=snapshot)) == legacy
    # Both new id-like fields and sort-relevant generations change; legacy row
    # order and normalized PK numbering must remain exactly the same.
    for table in metadata.tables.values():
        await db_session.execute(
            update(table).values(
                binding_id=uuid4(), binding_generation=-5, continuation_id="changed_new_runtime"
            )
        )
    changed = recorder.transcript(database=await recorder.database(db_session))
    assert json.loads(changed)["database_extensions"] != extensions
    assert RUNNER.legacy_transcript(changed) == legacy
    assert DATABASE_EXTENSIONS in snapshot, "transcript must not mutate its input snapshot"
    await db_session.execute(
        update(metadata.tables["tenant_ledger"]).values(delta_usd="1.234567890123")
    )
    priced = recorder.transcript(database=await recorder.database(db_session))
    assert RUNNER.legacy_transcript(priced) != legacy
    assert "1.234567890123" in priced, "legacy money remains exact and compared"


async def test_additive_columns_are_recorded_even_without_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = MetaData()
    for name in DB_TABLES:
        Table(
            name,
            metadata,
            *(Column(column, String) for column in LEGACY_DB_COLUMNS[name]),
            Column[UUID]("binding_id", Uuid),
        )
    monkeypatch.setattr(effect_recorder, "database_metadata", lambda: metadata)
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = Mock()
    session.execute.return_value.mappings.return_value = []
    recorder = EffectRecorder()
    raw = json.loads(recorder.transcript(database=await recorder.database(session)))
    assert raw["database_extensions"] == {
        name: {"columns": ["binding_id"], "rows": []} for name in DB_TABLES
    }


async def test_removing_a_legacy_column_fails_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = MetaData()
    Table("usage_events", metadata, Column("id", String))
    monkeypatch.setattr(effect_recorder, "database_metadata", lambda: metadata)
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError, match="Oracle legacy columns missing from usage_events"):
        await EffectRecorder().database(session)
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("section", ("effects", "requests", "database", "unexpected_section"))
def test_comparison_ignores_only_the_labelled_additive_section(section: str) -> None:
    original: dict[str, Json] = {"effects": [], "database": {"tenant_ledger": []}, "requests": []}
    expected = json.dumps(original, indent=2, sort_keys=True) + "\n"
    extended: dict[str, Json] = {
        **original,
        "database_extensions": {"thread_sessions": {"binding_id": "new"}},
    }
    assert RUNNER.legacy_transcript(json.dumps(extended)) == expected
    extended[section] = {"changed_legacy_value": "must remain visible"}
    assert RUNNER.legacy_transcript(json.dumps(extended)) != expected


def test_regeneration_writes_only_legacy_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy: dict[str, Json] = {"effects": [], "database": {"thread_sessions": []}, "requests": []}
    expected = json.dumps(legacy, indent=2, sort_keys=True) + "\n"
    transcript = json.dumps({**legacy, "database_extensions": {"thread_sessions": {"new": None}}})
    monkeypatch.setattr(RUNNER, "GOLDENS", tmp_path)

    def replay(name: str, *, mutation: str | None = None) -> str:
        return transcript

    monkeypatch.setattr(RUNNER, "replay", replay)
    RUNNER.check("probe", regen=True)
    assert (tmp_path / "probe.json").read_text() == expected
    RUNNER.check("probe")
