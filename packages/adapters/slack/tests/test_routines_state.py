"""Pure unit tests for daimon.adapters.slack.routines_panel.state.

Covers derive_glyph (all 4 precedence states) and picker_label (empty +
non-empty trigger_message). No I/O, no DB, no Slack SDK — these are
functional-core tests over a stdlib-only module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.adapters.slack.routines_panel.state import derive_glyph, picker_label
from daimon.core.stores.domain import RoutineRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(**kwargs: object) -> RoutineRow:
    """Build a RoutineRow with sensible defaults; override with kwargs."""
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": None,
        "agent_id": "agent-test-id",
        "agent_name": "Test Agent",
        "cron_expr": "0 * * * *",
        "timezone": "UTC",
        "trigger_message": "Hello world",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": None,
        "created_at": now,
        "updated_at": now,
    }
    base.update(kwargs)
    return RoutineRow(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# derive_glyph tests (4 precedence states)
# ---------------------------------------------------------------------------


def test_derive_glyph_paused_when_enabled_is_false() -> None:
    """Paused (enabled=False) takes highest precedence, even if last_fired_at is set."""
    row = _make_row(enabled=False, last_fired_at=datetime.now(UTC), last_error="some error")
    assert derive_glyph(row) == "⏸", (
        "disabled routine should return pause glyph regardless of run history"
    )


def test_derive_glyph_never_run_when_enabled_and_no_last_fired_at() -> None:
    """Never-run (enabled=True, last_fired_at=None) takes second precedence."""
    row = _make_row(enabled=True, last_fired_at=None, last_error=None)
    assert derive_glyph(row) == "⏳", (
        "enabled routine with no run history should return pending glyph"
    )


def test_derive_glyph_error_when_enabled_fired_and_last_error_set() -> None:
    """Error (enabled=True, last_fired_at set, last_error set) takes third precedence."""
    row = _make_row(
        enabled=True,
        last_fired_at=datetime.now(UTC),
        last_error="Timeout after 30s",
    )
    assert derive_glyph(row) == "❌", "fired routine with last_error should return error glyph"


def test_derive_glyph_success_when_enabled_fired_and_no_error() -> None:
    """Success (enabled=True, last_fired_at set, last_error=None) is the terminal case."""
    row = _make_row(
        enabled=True,
        last_fired_at=datetime.now(UTC),
        last_error=None,
        last_result_tail="Done.",
    )
    assert derive_glyph(row) == "✅", "fired routine with no error should return success glyph"


# ---------------------------------------------------------------------------
# picker_label tests
# ---------------------------------------------------------------------------


def test_picker_label_returns_hex_id_fallback_for_blank_trigger_message() -> None:
    """Blank trigger_message (whitespace-only) produces 'routine {id.hex[:8]}'."""
    row_id = uuid.UUID("12345678-1234-1234-1234-123456781234")
    row = _make_row(id=row_id, trigger_message="   ")
    assert picker_label(row) == "routine 12345678", (
        "blank trigger_message should produce the hex-id fallback label"
    )


def test_picker_label_returns_stripped_trigger_message() -> None:
    """Non-empty trigger_message is stripped and returned as the label."""
    row = _make_row(trigger_message="  Schedule a daily stand-up  ")
    assert picker_label(row) == "Schedule a daily stand-up", (
        "picker_label should strip whitespace from the trigger_message"
    )


def test_picker_label_truncates_long_trigger_message_at_60_chars() -> None:
    """trigger_message longer than 60 chars is truncated to exactly 60."""
    long_message = "A" * 100
    row = _make_row(trigger_message=long_message)
    result = picker_label(row)
    assert result == "A" * 60, "picker_label should truncate trigger_message to 60 characters"
    assert len(result) == 60, "truncated label must be exactly 60 characters long"
