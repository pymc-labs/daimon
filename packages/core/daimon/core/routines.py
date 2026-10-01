"""Rules the chat `routines` panels share: status glyph, label, order and who may act. Pure."""

from __future__ import annotations

from typing import Literal

from daimon.core.stores.domain import RoutineRow

PANEL_CAP = 25

Glyph = Literal["⏸", "⏳", "❌", "✅"]


def derive_glyph(row: RoutineRow) -> Glyph:
    """Single-glyph precedence: Paused > Never-run > Error > Success.

    ``record_result`` clears ``last_error`` on success, so
    ``last_error is not None`` always reflects the most recent run.
    """
    if not row.enabled:
        return "⏸"
    if row.last_fired_at is None:
        return "⏳"
    if row.last_error is not None:
        return "❌"
    return "✅"


def routine_label(row: RoutineRow) -> str:
    """The trigger message's first 60 characters, or a hex-id fallback when blank."""
    stripped = row.trigger_message.strip()
    if not stripped:
        return f"routine {row.id.hex[:8]}"
    return stripped[:60]


def panel_rows(rows: list[RoutineRow]) -> tuple[list[RoutineRow], int]:
    """Rows sorted by label and capped at `PANEL_CAP`, plus how many the cap hid."""
    ordered = sorted(rows, key=lambda row: routine_label(row).lower())
    return ordered[:PANEL_CAP], max(0, len(ordered) - PANEL_CAP)


def can_manage_routine(row: RoutineRow, *, user_id: str | None, is_admin: bool) -> bool:
    """An admin, or the routine's recorded creator.

    Fails closed on both nulls: a caller with no user id owns nothing, and an
    ownerless routine is admin-only.
    """
    return is_admin or (user_id is not None and row.created_by_user_id == user_id)
