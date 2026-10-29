"""Operator handover CLI input and preview behavior."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from daimon.adapters.cli.commands.routines import (
    read_seed_entries,
    run_link_source,
    run_seed_first_run,
)
from daimon.core.errors import UserFacingError
from daimon.core.routine_move import RoutineSeedEntry
from daimon.core.stores.routines import create_routine, get_routine, resume_routine
from daimon.testing.factories import make_tenant
from rich.console import Console

from ..harness import build_cli_runtime


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"source_routine_id": str(uuid.uuid4())}, "last_completed_slot"),
        (
            {
                "source_routine_id": str(uuid.uuid4()),
                "last_completed_slot": "2026-10-27T10:00:00",
            },
            "offset",
        ),
    ],
)
def test_invalid_entries_report_id(tmp_path, entry, message):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([entry]))
    with pytest.raises(UserFacingError, match=message) as error:
        read_seed_entries(path)
    assert entry["source_routine_id"] in str(error.value)
    assert "Nothing was changed" in str(error.value)


async def test_cli_preview_and_apply(db_session_factory):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            source_routine_id=uuid.uuid4(),
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="run",
            enabled=False,
        )
    entries = [
        RoutineSeedEntry.model_validate(
            dict(
                source_routine_id=row.source_routine_id,
                last_completed_slot="2026-10-27T10:00:00Z",
                source_next_run="2026-10-27T11:00:00Z",
                cron_expr="0 * * * *",
                timezone="UTC",
            )
        )
    ]
    output = StringIO()
    for apply in (False, True):
        await run_seed_first_run(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=output, width=300),
            tenant_id=tenant.id,
            entries=entries,
            apply=apply,
            max_age=timedelta(minutes=15),
            now=datetime(2026, 10, 27, 12, tzinfo=UTC),
        )
        async with db_session_factory() as session:
            stored = await get_routine(session, row.id, tenant_id=tenant.id)
            assert stored.enabled is apply
    rendered = output.getvalue()
    assert "T=2026-10-27T10:00:00+00:00" in rendered
    assert "cron=0 * * * * tz=UTC" in rendered
    assert "first_slot=2026-10-27T11:00:00+00:00" in rendered
    assert "roll forward unrun under skip" in rendered
    assert "Dry run: nothing written." in rendered
    assert "Applied atomically." in rendered


def test_empty_ids_file_is_refused_before_opening_runtime(tmp_path, monkeypatch):
    from daimon.adapters.cli.commands import routines

    path = tmp_path / "ids.json"
    path.write_text("[]")

    def unexpected_settings():
        raise AssertionError("empty selection reached runtime setup")

    monkeypatch.setattr(routines, "load_settings", unexpected_settings)
    with pytest.raises(UserFacingError, match="nonempty"):
        routines.export_rollback_command(tenant=uuid.uuid4(), ids_file=path)


@pytest.mark.parametrize("payload", [[], ["bad-id"], [str(uuid.UUID(int=1))] * 2])
def test_handback_invalid_ids_are_refused_before_runtime(tmp_path, monkeypatch, payload):
    from daimon.adapters.cli.commands import routines

    path = tmp_path / "ids.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(
        routines, "load_settings", lambda: pytest.fail("invalid ids reached runtime")
    )
    with pytest.raises(UserFacingError, match="Cannot read routine ids"):
        routines.pause_for_handback_command(tenant=uuid.uuid4(), ids_file=path)


async def test_handback_cli_preview_apply_and_preserve(db_session_factory):
    from daimon.adapters.cli.commands.routines import run_pause_for_handback

    next_run = datetime(2026, 10, 27, 13, tzinfo=UTC)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            source_routine_id=uuid.uuid4(),
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="run",
            enabled=False,
        )
        await resume_routine(
            session, row.id, tenant_id=tenant.id, now=next_run - timedelta(hours=1)
        )
    output = StringIO()
    for apply in (False, True, True):
        await run_pause_for_handback(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=output, width=300),
            tenant_id=tenant.id,
            source_ids=[row.source_routine_id],
            apply=apply,
            now=datetime(2026, 10, 27, 12, tzinfo=UTC),
        )
        async with db_session_factory() as session:
            stored = await get_routine(session, row.id, tenant_id=tenant.id)
            assert stored.enabled is not apply
    rendered = output.getvalue()
    assert "enabled=true expected_next_run=2026-10-27T13:00:00+00:00" in rendered
    assert "Dry run: nothing written." in rendered
    assert "Applied atomically." in rendered
    assert "already captured; preserved" in rendered


async def test_link_source_cli_preview_and_apply(db_session_factory):
    source_id = uuid.uuid4()
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="run",
            enabled=False,
        )
    output = StringIO()
    for apply in (False, True):
        await run_link_source(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=output, width=300),
            tenant_id=tenant.id,
            routine_id=row.id,
            source_id=source_id,
            apply=apply,
        )
        async with db_session_factory() as session:
            stored = await get_routine(session, row.id, tenant_id=tenant.id)
            assert stored.source_routine_id == (source_id if apply else None)
            assert stored.model_copy(update={"source_routine_id": None}) == row
    assert f"{row.id}: source_routine_id={source_id}" in output.getvalue()
    assert "Dry run: nothing written." in output.getvalue()
    assert "Applied atomically." in output.getvalue()
    with pytest.raises(UserFacingError, match="already linked to source routine"):
        await run_link_source(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=output),
            tenant_id=tenant.id,
            routine_id=row.id,
            source_id=source_id,
            apply=True,
        )


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
def test_link_source_command_options_and_one_line_refusal(monkeypatch, apply, refuse):
    from contextlib import asynccontextmanager

    from daimon.adapters.cli.commands import routines
    from typer.testing import CliRunner

    tenant_id, routine_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    @asynccontextmanager
    async def runtime(_settings):
        yield None

    async def run_link(**kwargs):
        assert kwargs["tenant_id"] == tenant_id
        assert kwargs["routine_id"] == routine_id
        assert kwargs["source_id"] == source_id
        assert kwargs["apply"] is apply
        if refuse:
            raise UserFacingError("routine is enabled; pause it before linking.")

    monkeypatch.setattr(routines, "load_settings", lambda: None)
    monkeypatch.setattr(routines, "build_runtime", runtime)
    monkeypatch.setattr(routines, "run_link_source", run_link)
    args = [
        "link-source",
        "--tenant",
        str(tenant_id),
        "--routine",
        str(routine_id),
        "--source-id",
        str(source_id),
    ]
    if apply:
        args.append("--apply")
    result = CliRunner().invoke(routines.routines_app, args)
    assert result.exit_code == (1 if refuse else 0), result.output
    if refuse:
        assert result.output.strip() == "Error: routine is enabled; pause it before linking."


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {},
        {"format_version": 1, "routines": []},
        {"format_version": 2, "routines": []},
        {"routines": []},
    ],
)
def test_seed_requires_nonempty_bare_array(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(UserFacingError, match="must be a nonempty array"):
        read_seed_entries(path)


@pytest.mark.parametrize("keep_source", [False, True])
def test_seed_array_accepts_only_source_fields(tmp_path: Path, keep_source: bool) -> None:
    values = {
        "source_routine_id": str(uuid.uuid4()),
        "last_completed_slot": "2026-10-27T10:00:00Z",
        "source_next_run": "2026-10-27T11:00:00Z",
        "cron_expr": "0 * * * *",
        "timezone": "UTC",
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([values]))
    assert read_seed_entries(path) == [RoutineSeedEntry.model_validate(values)]
    for field, legacy in (
        ("routine_id", "internal_routine_id"),
        ("next_run", "internal_next_run"),
    ):
        invalid = dict(values, **{legacy: values["source_" + field]})
        if not keep_source:
            invalid.pop("source_" + field)
        path.write_text(json.dumps([invalid]))
        with pytest.raises(UserFacingError, match=f"{legacy}: Extra inputs are not permitted"):
            read_seed_entries(path)


def test_seed_rejects_wrapped_source_entries(tmp_path: Path) -> None:
    values = {
        "source_routine_id": str(uuid.uuid4()),
        "last_completed_slot": "2026-10-27T10:00:00Z",
        "source_next_run": "2026-10-27T11:00:00Z",
        "cron_expr": "0 * * * *",
        "timezone": "UTC",
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"format_version": 2, "routines": [values]}))
    with pytest.raises(UserFacingError, match="must be a nonempty array"):
        read_seed_entries(path)
