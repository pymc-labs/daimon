"""Small, local inline SVG set for MCP pages.

Lucide icons are ISC licensed; the GitHub mark is from Feather (MIT), since
Lucide no longer includes brand marks. See static/Lucide-LICENSE.txt and
static/Feather-LICENSE.txt.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")

_NAMES = frozenset(
    {
        "building",
        "check",
        "chevron-left",
        "chevron-right",
        "circle-check",
        "clock",
        "eye",
        "github",
        "hourglass",
        "info",
        "link",
        "pencil",
        "search",
        "triangle-alert",
        "user",
    }
)


@lru_cache(maxsize=len(_NAMES))
def _source(name: str) -> str:
    if name not in _NAMES:
        raise ValueError(f"Unknown web icon: {name}")
    return (STATIC_DIR / "lucide" / f"{name}.svg").read_text()


def icon(name: str, *, label: str | None = None) -> str:
    """Render a fixed icon inline, hidden when adjacent text names its action."""
    accessible = (
        f'role="img" aria-label="{html.escape(label, quote=True)}"'
        if label is not None
        else 'aria-hidden="true" focusable="false"'
    )
    return _source(name).replace("<svg", f'<svg class="web-icon web-icon--{name}" {accessible}', 1)
