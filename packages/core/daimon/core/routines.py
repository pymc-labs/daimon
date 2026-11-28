"""Rules the chat `routines` panels share: status glyph, label, order and who may act. Pure."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final, Literal

from daimon.core.fire.subject import RoutineFacts
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.workflows import WorkflowRow

PANEL_CAP = 25
#: A moved routine's line on every panel; the panel links the workflow and offers no changes.
MOVED_TO_WORKFLOWS: Final = "Moved to Workflows. Change it there."
#: A routine run's failure, where its output is private to someone else.
PRIVATE_FAILED: Final = "Private routine run failed."
#: A routine surface's badge: routines still run, workflows replace them.
LEGACY: Final = "Legacy"
#: The same, where there is room to name the replacement.
LEGACY_USE_WORKFLOWS: Final = "Legacy, use Workflows"
#: The admin's button, on every surface that lists routines with actions.
MOVE_TO_WORKFLOW: Final = "Move to a workflow"

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


def mounts_personal_memory(row: RoutineFacts) -> bool:
    """Whether a headless fire mounts its owner's personal memory, read-only.

    Only a routine whose result reaches nobody but its owner: no destination
    and no channel. Decided from the row at each fire, so a destination added
    later stops it at the next run. An owner-DM routine runs as a live DM turn
    and gets personal memory there, not through this headless decision.
    """
    return _reaches_nobody(row.destination_kind, row.destination_id, row.channel_id)


def _reaches_nobody(kind: str | None, destination_id: str | None, channel_id: str | None) -> bool:
    return kind is None and destination_id is None and channel_id is None


def private_destination(
    kind: str | None, destination_id: str | None, channel_id: str | None
) -> bool:
    """DM results and destination-free runs, which mount personal memory."""
    return kind == "dm" or _reaches_nobody(kind, destination_id, channel_id)


def has_private_output(row: RoutineFacts) -> bool:
    """DM results and destination-free runs, which mount personal memory."""
    return private_destination(row.destination_kind, row.destination_id, row.channel_id)


def hides_moved_output(
    workflow: WorkflowRow, moved: Mapping[str, Any], *, reader: uuid.UUID | None
) -> bool:
    """A moved routine's stored text (`converted_from`), private as the routine was when it moved,
    whatever the workflow's spec says now."""
    if reader is not None and reader == workflow.owner_account_id:
        return False
    to: Mapping[str, Any] = moved.get("to") or {}
    return private_destination(to.get("kind"), to.get("id"), moved.get("channel_id"))


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
            "last_error": PRIVATE_FAILED if row.last_error is not None else None,
            "delivery_payload": None,
            "delivery_note": None,
        }
    )
