"""A core `ui.Panel` drawn as Adaptive Card elements."""

from __future__ import annotations

from typing import Any

from daimon.adapters.teams.ui_render import panel_body
from daimon.core import ui


def _dump(panel: ui.Panel) -> list[dict[str, Any]]:
    return [e.model_dump(by_alias=True, exclude_none=True) for e in panel_body(panel)]


def _text(text: str, **style: object) -> dict[str, object]:
    return {"type": "TextBlock", "text": text, "wrap": True} | style


def test_a_panel_is_a_heading_then_each_section_set_apart() -> None:
    panel = ui.Panel(
        "Billing",
        (
            ui.Section((ui.Text(("$1.00 used",)),), title="You", flag="⚠️ Over"),
            ui.Section((ui.Figure("$9.00", "total credit left"), ui.Muted(("a",)))),
            ui.Section((ui.Text(("x",)),), element_id="hid", hidden=True),
        ),
        subtitle=("May 2026",),
    )
    grey = {"isSubtle": True, "size": "Small", "spacing": "None"}
    header, you, credit, hidden = _dump(panel)
    assert header == {
        "type": "Container",
        "items": [_text("Billing", weight="Bolder", size="Medium"), _text("May 2026", **grey)],
    }
    assert you == {
        "type": "Container",
        "separator": True,
        "spacing": "Large",
        "items": [_text("You  ⚠️ Over", weight="Bolder"), _text("$1.00 used", spacing="Small")],
    }
    assert credit["items"] == [
        {"type": "TextBlock", "text": "$9.00", "size": "ExtraLarge", "weight": "Bolder"},
        _text("total credit left", spacing="None"),
        _text("a", **grey),
    ], "the figure is the biggest text on the card"
    assert hidden["id"] == "hid" and hidden["isVisible"] is False
