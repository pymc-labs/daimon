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
    if row.last_error is not None or (
        row.destination_kind == "dm" and row.delivery_status == "skipped"
    ):
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


def has_private_output(row: RoutineRow) -> bool:
    """Legacy destination-free runs could mount the owner's personal memory."""
    return row.destination_kind == "dm" or (
        row.destination_kind is None and row.destination_id is None and row.channel_id is None
    )


def can_read_routine_output(row: RoutineRow, *, user_id: str | None) -> bool:
    """Private output requires an identified owner, including for admins."""
    return not has_private_output(row) or (
        user_id is not None and row.created_by_user_id == user_id
    )


def routine_for_reader(row: RoutineRow, *, user_id: str | None) -> RoutineRow:
    """Keep management metadata while removing private previews and diagnostics."""
    if can_read_routine_output(row, user_id=user_id):
        return row
    return row.model_copy(
        update={
            "last_result_tail": None,
            "last_error": "Private routine run failed." if row.last_error is not None else None,
            "delivery_payload": None,
            "delivery_note": None,
        }
    )
