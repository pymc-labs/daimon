"""A core `ui.Panel` as Block Kit blocks, for a modal whose own title is the panel's.

The subtitle is a context block; each section is a mrkdwn section with its
grey rows in a context under it, set apart by dividers. Slack has no accent,
so the tone is not drawn.
"""

from __future__ import annotations

from typing import Any

from daimon.core import ui


def context(text: str) -> dict[str, Any]:
    """Small grey lines."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def mrkdwn_section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def section_blocks(section: ui.Section) -> list[dict[str, Any]]:
    flag = f"  {section.flag}" if section.flag else ""
    main = [f"*{section.title}*{flag}"] if section.title else []
    grey: list[str] = []
    for block in section.blocks:
        if isinstance(block, ui.Figure):
            main.append(f"*{block.value}*" + (f" {block.caption}" if block.caption else ""))
        elif isinstance(block, ui.Text):
            main += block.lines
        else:
            grey += block.lines
    blocks = [mrkdwn_section("\n".join(main))] if main else []
    return blocks + ([context("\n".join(grey))] if grey else [])


def panel_blocks(panel: ui.Panel) -> list[dict[str, Any]]:
    """The subtitle, then each section after a divider."""
    blocks = [context("\n".join(panel.subtitle))] if panel.subtitle else []
    for section in panel.sections:
        blocks += [{"type": "divider"}] if blocks else []
        blocks += section_blocks(section)
    return blocks
