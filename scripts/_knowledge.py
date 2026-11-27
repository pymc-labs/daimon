"""Markers around docs text that is true only where the web app is on.

The docs are also what an agent reads to answer questions about Daimon (the
`self_knowledge` feature). It drops everything between these two lines for an agent
whose `web_awareness` is Off. A whole web-only page says `knowledge: web` in its front
matter instead. The generators wrap their web-only rows and sections with `web_only`.
"""

from __future__ import annotations

WEB_OPEN = "<!-- knowledge: web -->"
WEB_CLOSE = "<!-- /knowledge -->"


def web_only(lines: list[str]) -> list[str]:
    """`lines` between the markers, set off by blank lines so any table inside stays valid."""
    return [WEB_OPEN, *lines, WEB_CLOSE]
