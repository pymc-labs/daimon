"""Tests for routines_panel: load_routines (real DB) + build_content_view (pure).

Covers:
- load_routines returns entries with correct glyphs across 4 routine states.
- load_routines caps at 25 entries and counts the overflow.
- build_content_view renders one section-with-overflow per entry; text is escaped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.adapters.slack.routines_panel.read import _PICKER_CAP, load_routines
from daimon.adapters.slack.routines_panel.state import RoutinesPanelState
from daimon.adapters.slack.routines_panel.views import build_content_view
from daimon.core._models import Routine as RoutineOrm  # ORM escape-hatch for seeding
from daimon.core._models import Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import create_routine
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_ROUTINES_PANEL"


async def _seed_tenant(session: AsyncSession, *, workspace_id: str = _TEAM_ID) -> uuid.UUID:
    """Create a slack Tenant row and flush. Returns the tenant UUID."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=workspace_id)
    session.add(Tenant(id=tenant_id, platform="slack", external_id=workspace_id))
    await session.flush()
    return tenant_id


async def _set_routine_state(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    last_fired_at: datetime | None,
    last_error: str | None,
) -> None:
    """Directly update last_fired_at and last_error for a routine row.

    Uses the ORM escape-hatch (permitted in tests per guideline:testing) to
    set fields that core stores don't expose via their public API.
    """
    await session.execute(
        update(RoutineOrm)
        .where(RoutineOrm.id == routine_id)
        .values(last_fired_at=last_fired_at, last_error=last_error)
    )
    await session.flush()


# ---------------------------------------------------------------------------
# load_routines tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_routines_returns_entries_with_correct_glyphs_for_all_4_states(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """load_routines correctly derives the glyph for all 4 routine states."""
    tenant_id = await _seed_tenant(db_session)
    now = datetime.now(UTC)

    # Paused: enabled=False
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="a1",
        agent_name="Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="paused routine",
        enabled=False,
    )
    # Never-run: enabled=True, last_fired_at=None (default)
    await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="a2",
        agent_name="Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="never run routine",
        enabled=True,
    )
    # Error: enabled=True + last_fired_at set + last_error set
    error_routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="a3",
        agent_name="Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="errored routine",
        enabled=True,
    )
    await _set_routine_state(db_session, error_routine.id, last_fired_at=now, last_error="oops")
    # Success: enabled=True + last_fired_at set + last_error=None
    success_routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="a4",
        agent_name="Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="successful routine",
        enabled=True,
    )
    await _set_routine_state(db_session, success_routine.id, last_fired_at=now, last_error=None)
    await db_session.flush()

    fake_anthropic = build_fake_anthropic(make_fake_ma_handler())
    async with db_session_factory() as session:
        entries, over_cap_count, _ = await load_routines(
            session, fake_anthropic, tenant_id=tenant_id
        )

    assert over_cap_count == 0, "4 routines should not exceed the 25-entry cap"
    glyph_by_label = {e.label: e.glyph for e in entries}

    assert glyph_by_label.get("paused routine") == "⏸", (
        "disabled routine should produce the pause glyph"
    )
    assert glyph_by_label.get("never run routine") == "⏳", (
        "never-fired routine should produce the pending glyph"
    )
    assert glyph_by_label.get("errored routine") == "❌", (
        "routine with last_error should produce the error glyph"
    )
    assert glyph_by_label.get("successful routine") == "✅", (
        "routine with no last_error after firing should produce the success glyph"
    )


@pytest.mark.asyncio
async def test_load_routines_caps_at_25_and_reports_over_cap_count(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """load_routines caps entries at 25 and returns the remainder as over_cap_count."""
    tenant_id = await _seed_tenant(db_session, workspace_id="T_ROUTINES_CAP")
    total = _PICKER_CAP + 3  # 28 routines

    for i in range(total):
        await create_routine(
            db_session,
            tenant_id=tenant_id,
            created_by_user_id=None,
            agent_id=f"agent-{i}",
            agent_name="Agent",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message=f"routine {i:04d}",
        )
    await db_session.flush()

    fake_anthropic = build_fake_anthropic(make_fake_ma_handler())
    async with db_session_factory() as session:
        entries, over_cap_count, _ = await load_routines(
            session, fake_anthropic, tenant_id=tenant_id
        )

    assert len(entries) == _PICKER_CAP, f"load_routines must cap entries at {_PICKER_CAP}"
    assert over_cap_count == 3, "over_cap_count must equal the number of entries beyond the cap"


# ---------------------------------------------------------------------------
# build_content_view tests
# ---------------------------------------------------------------------------


def test_build_content_view_every_entry_has_overflow_accessory() -> None:
    """Each routine section block must have an overflow accessory with the correct action_id."""
    routine_id = uuid.uuid4()
    # Build a minimal state with one RoutineEntry
    from daimon.adapters.slack.routines_panel.state import RoutineEntry

    now = datetime.now(UTC)
    from daimon.core.stores.domain import RoutineRow

    row = RoutineRow(
        id=routine_id,
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
    entry = RoutineEntry(routine=row, agent_name="Alpha", glyph="⏳", label="My routine")
    state = RoutinesPanelState(rows=[entry], over_cap_count=0, agent_name_map={})

    view = build_content_view(state)

    # Find section blocks (not the actions/refresh block)
    section_blocks: list[dict[str, Any]] = [b for b in view["blocks"] if b.get("type") == "section"]
    assert len(section_blocks) == 1, "one section block expected for one entry"

    section = section_blocks[0]
    accessory = section.get("accessory")
    assert accessory is not None, "section block must have an accessory"
    assert accessory.get("type") == "overflow", "accessory must be an overflow element"
    assert accessory.get("action_id", "").startswith("routine_action:"), (
        "overflow action_id must start with 'routine_action:'"
    )
    assert str(routine_id) in accessory["action_id"], (
        "overflow action_id must contain the routine UUID"
    )


def test_build_content_view_escapes_label_and_agent_name_in_mrkdwn() -> None:
    """Label and agent_name containing mrkdwn special chars are escaped."""
    from daimon.adapters.slack.routines_panel.state import RoutineEntry
    from daimon.core.stores.domain import RoutineRow

    now = datetime.now(UTC)
    row = RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=None,
        agent_id="agent-y",
        agent_name="<Test> & Agent",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="<Alert> & notify",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=now,
        updated_at=now,
    )
    # The label is the picker_label (picked from trigger_message), agent_name is what we pass
    entry = RoutineEntry(
        routine=row,
        agent_name="<Test> & Agent",
        glyph="✅",
        label="<Alert> & notify",
    )
    state = RoutinesPanelState(rows=[entry], over_cap_count=0, agent_name_map={})

    view = build_content_view(state)

    section_blocks = [b for b in view["blocks"] if b.get("type") == "section"]
    text = section_blocks[0]["text"]["text"]
    # The label and agent_name must be escaped
    assert "&lt;Alert&gt;" in text, "< and > in label must be mrkdwn-escaped"
    assert "&amp;" in text, "& in label must be mrkdwn-escaped"
    assert "&lt;Test&gt;" in text, "< and > in agent_name must be mrkdwn-escaped"
    assert "<Alert>" not in text, "raw < in label must not appear in the mrkdwn text"
