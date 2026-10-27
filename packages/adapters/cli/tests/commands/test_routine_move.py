"""Operator handover CLI input and preview behavior."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from daimon.adapters.cli.commands.routines import (
    read_seed_entries,
    run_seed_first_run,
)
from daimon.core.errors import UserFacingError
from daimon.core.routine_move import RoutineSeedEntry
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from rich.console import Console

from ..harness import build_cli_runtime


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"internal_routine_id": str(uuid.uuid4())}, "last_completed_slot"),
        (
            {
                "internal_routine_id": str(uuid.uuid4()),
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
    assert entry["internal_routine_id"] in str(error.value)
    assert "Nothing was changed" in str(error.value)


async def test_cli_preview_and_apply(db_session_factory):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="ag",
            internal_routine_id=uuid.uuid4(),
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="run",
            enabled=False,
        )
    entries = [
        RoutineSeedEntry.model_validate(
            dict(
                internal_routine_id=row.internal_routine_id,
                last_completed_slot="2026-10-27T10:00:00Z",
                internal_next_run="2026-10-27T11:00:00Z",
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
