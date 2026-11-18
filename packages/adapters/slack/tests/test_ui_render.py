"""A core `ui.Panel` drawn as Block Kit blocks."""

from __future__ import annotations

from daimon.adapters.slack.ui_render import panel_blocks
from daimon.core import ui


def test_a_panel_is_its_subtitle_then_each_section_after_a_divider() -> None:
    panel = ui.Panel(
        "Billing",
        (
            ui.Section((ui.Text(("$1.00 used",)),), title="You", flag="⚠️ Over"),
            ui.Section((ui.Figure("$9.00", "total credit left"), ui.Muted(("a", "b")))),
            ui.Section((ui.Figure("No credit left"),)),
        ),
        subtitle=("May 2026", "$1.00 spent"),
    )
    assert panel_blocks(panel) == [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "May 2026\n$1.00 spent"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*You*  ⚠️ Over\n$1.00 used"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*$9.00* total credit left"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "a\nb"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*No credit left*"}},
    ], "the flag sits outside the bold title; grey rows are a context under their section"
    untitled = ui.Panel("T", (ui.Section((ui.Text(("x",)),)),))
    assert panel_blocks(untitled) == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "x"}}
    ], "no subtitle, no divider above the first section"
