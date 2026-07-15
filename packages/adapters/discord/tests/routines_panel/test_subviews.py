"""Tests for routines_panel.subviews — last-output container + Back button."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import discord

# pyright: reportPrivateUsage=false
from daimon.adapters.discord.routines_panel.subviews import (
    ViewLastOutputSubView,
    _BackButton,
    build_last_output_container,
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
        "last_fired_at": datetime(2026, 5, 1, tzinfo=UTC),
        "last_error": None,
        "last_result_tail": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RoutineRow.model_validate(base)


def _collect_text_display_content(container: discord.ui.Container[Any]) -> list[str]:
    """Walk container children and collect .content for every TextDisplay."""
    result: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            result.append(child.content)
    return result


# ---------------------------------------------------------------------------
# V2 container builder tests (build_last_output_container)
# ---------------------------------------------------------------------------


def test_last_output_container_header_contains_last_output_title() -> None:
    row = _make_row(last_result_tail="hi")
    container = build_last_output_container(row)
    texts = _collect_text_display_content(container)
    assert any("Last output" in t for t in texts), "header TextDisplay must contain 'Last output'"


def test_last_output_container_has_fenced_code_block() -> None:
    row = _make_row(last_result_tail="some output here", last_error=None)
    container = build_last_output_container(row)
    texts = _collect_text_display_content(container)
    body_texts = [t for t in texts if "```" in t]
    assert body_texts, "container must contain a fenced code block TextDisplay"
    assert "some output here" in body_texts[0], (
        "fenced code block must include the last_result_tail content"
    )


def test_last_output_container_shows_error_body_on_failure() -> None:
    row = _make_row(last_error="Traceback: BOOM", last_result_tail=None)
    container = build_last_output_container(row)
    texts = _collect_text_display_content(container)
    assert any("Traceback: BOOM" in t for t in texts), (
        "container must render the last_error content in the code block"
    )


def test_last_output_container_truncates_at_1000_chars() -> None:
    long_text = "x" * 2000
    row = _make_row(last_result_tail=long_text, last_error=None)
    container = build_last_output_container(row)
    texts = _collect_text_display_content(container)
    body_texts = [t for t in texts if "```" in t]
    assert body_texts, "container must have a fenced code block"
    body = body_texts[0]
    assert "… (truncated)" in body, "truncated body must carry a marker"
    # Total body text must be well under 4000 (V2 aggregate limit)
    assert len(body) < 4000, "truncated container body must stay under V2 text limit"


# ---------------------------------------------------------------------------
# ViewLastOutputSubView (LayoutView) tests
# ---------------------------------------------------------------------------


def test_back_button_present_on_subview() -> None:
    row = _make_row()
    view = ViewLastOutputSubView(row, allowed_user_id=42)
    back_buttons = [c for c in view.walk_children() if isinstance(c, _BackButton)]
    assert len(back_buttons) == 1, "sub-view must carry exactly one ← Back button"
    button: discord.ui.Button[Any] = back_buttons[0]
    assert button.label == "← Back"


def test_subview_is_layout_view() -> None:
    row = _make_row()
    view = ViewLastOutputSubView(row, allowed_user_id=42)
    assert isinstance(view, discord.ui.LayoutView), (
        "ViewLastOutputSubView must be a LayoutView (V2)"
    )
