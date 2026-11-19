"""A platform-neutral panel: what a chat panel says, before any platform draws it.

A core builder (`daimon.core.billing_panel.billing_screen`) decides the words,
their order and their grouping once; each adapter's `ui_render` draws a
`Panel` with its own primitives (Discord V2 components, Slack Block Kit,
Teams Adaptive Cards). A deliberate difference between platforms is an
argument to the builder, never a second copy of the copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tone = Literal["alert", "warning"]


@dataclass(frozen=True)
class Figure:
    """The panel's big figure, with the words under it."""

    value: str
    caption: str | None = None


@dataclass(frozen=True)
class Text:
    """Body rows, close together."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class Muted:
    """Small grey rows; always a section's last block."""

    lines: tuple[str, ...]


Block = Figure | Text | Muted


@dataclass(frozen=True)
class Section:
    """One part of a panel, set apart from the one above, under an optional bold title.

    ``flag`` is a warning beside the title (`⚠️ Over your monthly limit`). A
    ``hidden`` section is drawn only where a toggle reveals it (Teams), by
    ``element_id``.
    """

    blocks: tuple[Block, ...]
    title: str | None = None
    flag: str | None = None
    element_id: str | None = None
    hidden: bool = False


@dataclass(frozen=True)
class Panel:
    """A titled panel: grey subtitle rows, then its sections; ``tone`` colours an accent."""

    title: str
    sections: tuple[Section, ...]
    subtitle: tuple[str, ...] = ()
    tone: Tone | None = None


def titled(title: str, *lines: str, more: str | None = None) -> Section:
    """A titled section of rows, with an optional grey `+ N more` row under them."""
    blocks: tuple[Block, ...] = (Text(lines),) if more is None else (Text(lines), Muted((more,)))
    return Section(blocks, title=title)
