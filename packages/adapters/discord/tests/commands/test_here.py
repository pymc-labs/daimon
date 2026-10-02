"""The Discord card stays one ephemeral layout when its text is long."""

from __future__ import annotations

import discord
from daimon.adapters.discord.commands.here import build_here_view


def test_long_card_stays_inside_total_components_limit() -> None:
    view = build_here_view("x" * 8000)
    displays = [item for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
    assert len(displays) == 1
    assert len(displays[0].content) == 3900
