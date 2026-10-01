"""Pure builders for the `routines` panel."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from daimon.adapters.teams.routines_card import form_values, output_card
from daimon.core.stores.domain import RoutineRow


def _row(*, last_error: str | None, last_result_tail: str | None) -> RoutineRow:
    now = datetime.now(UTC)
    return RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id="user-a",
        agent_id="agent_1",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="standup",
        enabled=True,
        next_fire_at=None,
        last_fired_at=now,
        last_error=last_error,
        last_result_tail=last_result_tail,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("error", "tail", "shown"),
    [
        ("TurnError: boom", "old output", "TurnError: boom"),
        (None, "done", "done"),
        (None, None, "(no output)"),
    ],
)
def test_output_card_shows_the_last_error_before_the_output(
    error: str | None, tail: str | None, shown: str
) -> None:
    card = json.dumps(output_card(_row(last_error=error, last_result_tail=tail)).model_dump())
    assert shown in card, "the most recent run's error wins over any output"
    assert error is None or "old output" not in card, "a failed run hides the stale output"


def test_form_values_trims_fields_and_fills_missing_ones() -> None:
    values = form_values({"action": "routine_create", "agent": " daimon ", "cron": None})
    assert values == {"agent": "daimon", "cron": "", "timezone": "", "message": ""}
    assert form_values("not a form") == {"agent": "", "cron": "", "timezone": "", "message": ""}
