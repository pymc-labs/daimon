"""A core `ui.Panel` as Adaptive Card elements.

The title is a heading over grey subtitle rows; each section is a container
set apart from the one above, its figure the biggest text on the card. Teams
has no accent, so the tone is not drawn.
"""

from __future__ import annotations

from daimon.adapters.teams.card_actions import heading
from daimon.core import ui
from microsoft_teams.cards import CardElement, Container, TextBlock


def muted(*lines: str) -> list[CardElement]:
    """Small grey lines."""
    return [
        TextBlock(text=line, is_subtle=True, size="Small", spacing="None", wrap=True)
        for line in lines
    ]


def section_container(section: ui.Section) -> Container:
    items: list[CardElement] = []
    if section.title:
        flag = f"  {section.flag}" if section.flag else ""
        items.append(TextBlock(text=section.title + flag, weight="Bolder", wrap=True))
    for block in section.blocks:
        if isinstance(block, ui.Figure):
            items.append(TextBlock(text=block.value, size="ExtraLarge", weight="Bolder"))
            if block.caption is not None:
                items.append(TextBlock(text=block.caption, spacing="None", wrap=True))
        elif isinstance(block, ui.Text):
            items += [TextBlock(text=line, spacing="Small", wrap=True) for line in block.lines]
        else:
            items += muted(*block.lines)
    return Container(
        items=items,
        separator=True,
        spacing="Large",
        id=section.element_id,
        is_visible=False if section.hidden else None,
    )


def panel_body(panel: ui.Panel) -> list[CardElement]:
    """The heading and subtitle, then each section."""
    header = Container(items=[heading(panel.title), *muted(*panel.subtitle)])
    return [header, *(section_container(section) for section in panel.sections)]
