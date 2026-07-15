"""Tests for the Delete-routine overflow option + build_delete_confirm_modal.

Covers:
- The per-routine overflow menu now offers a "Delete" option (value=delete).
- build_delete_confirm_modal shape: callback_id, Delete/Cancel buttons, the
  label section (escaped), and the private_metadata round-trip carrying
  team_id / channel_id / routine_id / root_view_id.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from daimon.adapters.slack.routines_panel.state import RoutineEntry, RoutinesPanelState
from daimon.adapters.slack.routines_panel.views import (
    build_content_view,
    build_delete_confirm_modal,
)
from daimon.core.stores.domain import RoutineRow


def _entry() -> RoutineEntry:
    now = datetime.now(UTC)
    row = RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=None,
        agent_id="agent-x",
        agent_name="Alpha",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="My routine",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=now,
        updated_at=now,
    )
    return RoutineEntry(routine=row, agent_name="Alpha", glyph="⏳", label="My routine")


def test_overflow_menu_includes_delete_option() -> None:
    """Each routine row's overflow menu must offer a Delete option (value=delete)."""
    state = RoutinesPanelState(rows=[_entry()], over_cap_count=0, agent_name_map={})

    view = build_content_view(state)

    section = next(b for b in view["blocks"] if b.get("type") == "section")
    values = [o["value"] for o in section["accessory"]["options"]]
    assert "delete" in values, "overflow menu must offer a Delete option"


def test_build_delete_confirm_modal_has_callback_id_and_delete_submit() -> None:
    view = build_delete_confirm_modal(
        team_id="T1",
        channel_id="C1",
        routine_id=str(uuid.uuid4()),
        root_view_id="V_ROOT",
        label="daily stand-up",
    )

    assert view["type"] == "modal", "confirm modal must be type modal"
    assert view["callback_id"] == "routines__delete_confirm", (
        "callback_id must be routines__delete_confirm so app.py dispatch routes it"
    )
    assert view["submit"]["text"] == "Delete", "submit button must read Delete"
    assert view["close"]["text"] == "Cancel", "close button must read Cancel"
    assert len(view["title"]["text"]) <= 24, "modal title must be <= 24 chars (Slack cap)"


def test_build_delete_confirm_modal_body_shows_escaped_label() -> None:
    view = build_delete_confirm_modal(
        team_id="T1",
        channel_id="C1",
        routine_id=str(uuid.uuid4()),
        root_view_id="V_ROOT",
        label="<Alert> & notify",
    )

    body = view["blocks"][0]["text"]["text"]
    assert "&lt;Alert&gt;" in body, "label must be mrkdwn-escaped in the confirm body"
    assert "&amp;" in body, "ampersand in label must be mrkdwn-escaped"
    assert "<Alert>" not in body, "raw < in label must not appear in the confirm body"


def test_build_delete_confirm_modal_private_metadata_round_trips() -> None:
    routine_id = str(uuid.uuid4())
    view = build_delete_confirm_modal(
        team_id="T_RT",
        channel_id="C_RT",
        routine_id=routine_id,
        root_view_id="V_RT",
        label="x",
    )

    meta = json.loads(view["private_metadata"])
    assert meta["team_id"] == "T_RT", "team_id must round-trip through private_metadata"
    assert meta["channel_id"] == "C_RT", "channel_id must round-trip through private_metadata"
    assert meta["routine_id"] == routine_id, "routine_id must round-trip through private_metadata"
    assert meta["root_view_id"] == "V_RT", "root_view_id must round-trip through private_metadata"
