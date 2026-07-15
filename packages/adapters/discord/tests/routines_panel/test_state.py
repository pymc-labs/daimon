"""Tests for routines_panel.state — derive_state reducer + state mutation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from daimon.adapters.discord.routines_panel.state import (
    RoutineEntry,
    RoutinesPanelState,
    derive_state,
)
from daimon.core.stores.domain import RoutineRow


def _make_row(**overrides: Any) -> RoutineRow:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "platform": "discord",
        "guild_id": "G1",
        "created_by_user_id": None,
        "agent_id": "agent_a",
        "agent_name": "daimon",
        "cron_expr": "0 9 * * 1-5",
        "timezone": "UTC",
        "trigger_message": "summarize",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RoutineRow.model_validate(base)


def _make_entry(row: RoutineRow) -> RoutineEntry:
    glyph, color = derive_state(row)
    return RoutineEntry(
        routine=row,
        agent_name="agent",
        glyph=glyph,
        color=color,
        label=row.trigger_message[:60],
    )


def test_derive_state_paused() -> None:
    row = _make_row(
        enabled=False,
        last_fired_at=datetime(2026, 5, 1, tzinfo=UTC),
        last_error="boom",
    )
    glyph, color = derive_state(row)
    assert glyph == "⏸", "paused must take precedence over error"
    assert color == 0xFEE75C, "paused color must be yellow"


def test_derive_state_never_run() -> None:
    row = _make_row(enabled=True, last_fired_at=None, last_error=None)
    glyph, color = derive_state(row)
    assert glyph == "⏳", "enabled + never-run must yield hourglass"
    assert color == 0x5865F2, "never-run color must be blue"


def test_derive_state_never_run_beats_error() -> None:
    row = _make_row(enabled=True, last_fired_at=None, last_error="boom")
    glyph, color = derive_state(row)
    assert glyph == "⏳", "never-run must outrank error in the precedence chain"
    assert color == 0x5865F2


def test_derive_state_error() -> None:
    row = _make_row(enabled=True, last_fired_at=datetime(2026, 5, 1, tzinfo=UTC), last_error="boom")
    glyph, color = derive_state(row)
    assert glyph == "❌", "errored fired routine must show red"
    assert color == 0xED4245


def test_derive_state_success() -> None:
    row = _make_row(enabled=True, last_fired_at=datetime(2026, 5, 1, tzinfo=UTC), last_error=None)
    glyph, color = derive_state(row)
    assert glyph == "✅", "successful fired routine must show green"
    assert color == 0x57F287


def test_derive_state_paused_beats_never_run() -> None:
    row = _make_row(enabled=False, last_fired_at=None)
    glyph, color = derive_state(row)
    assert glyph == "⏸", "paused must outrank never-run"
    assert color == 0xFEE75C


def test_state_select_updates_selected() -> None:
    rows = [_make_entry(_make_row()) for _ in range(3)]
    state = RoutinesPanelState.initial(rows=rows, over_cap_count=0, agent_name_map={})
    state.select(rows[1].routine.id)
    assert state.selected == rows[1], "select must move the cursor to the matching entry"


def test_state_select_unknown_id_is_noop() -> None:
    rows = [_make_entry(_make_row())]
    state = RoutinesPanelState.initial(rows=rows, over_cap_count=0, agent_name_map={})
    state.select(uuid.uuid4())
    assert state.selected == rows[0], "selecting an unknown id must leave selection unchanged"
