"""Small, local inline SVG set for MCP pages.

Lucide utility icons are ISC licensed. Brand marks come from Simple Icons
at 98820a4; see static/Lucide-LICENSE.txt, static/SimpleIcons-LICENSE.txt,
and static/SimpleIcons-NOTICE.txt. Brand shapes are used unmodified.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")

_NAMES = frozenset(
    {
        "building",
        "chevron-left",
        "chevron-right",
        "circle-check",
        "clock",
        "discord",
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

_BRAND_NAMES = frozenset({"discord", "github"})


@lru_cache(maxsize=len(_NAMES))
def _source(name: str) -> str:
    if name not in _NAMES:
        raise ValueError(f"Unknown web icon: {name}")
    source = "simple-icons" if name in _BRAND_NAMES else "lucide"
    return (STATIC_DIR / source / f"{name}.svg").read_text()


def icon(name: str, *, label: str | None = None) -> str:
    """Render a fixed icon inline, hidden when adjacent text names its action."""
    accessible = (
        f'role="img" aria-label="{html.escape(label, quote=True)}"'
        if label is not None
        else 'aria-hidden="true" focusable="false"'
    )
    return _source(name).replace("<svg", f'<svg class="web-icon web-icon--{name}" {accessible}', 1)


def platform_mark(platform: str | None) -> str:
    """Teams has no Simple Icons mark; Slack's mark was withdrawn."""
    return icon(platform) if platform in {"discord"} else ""
