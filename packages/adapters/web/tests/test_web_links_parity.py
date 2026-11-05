"""The bot's link builder agrees with the web app: one route table, one role table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from daimon.adapters.web import access
from daimon.core.web_links import DEEP_LINKS, MEMBER_CAPS

ROOT = Path(__file__).resolve().parents[4]


def test_table_equals_the_web_apps_route_table() -> None:
    """Both sides read one table: drifting either fails here."""
    web = json.loads((ROOT / "web/src/lib/deep-links.json").read_text())
    ours = {
        key: {"path": link.path, "param": link.param, "module": link.module, "cap": link.cap}
        for key, link in DEEP_LINKS.items()
    }
    assert ours == web, "core/web_links.py and web/src/lib/deep-links.json must match"


@pytest.mark.parametrize("role", ["member", "admin"])
def test_member_capabilities_match_the_web_role_table(role: str) -> None:
    """The bot's idea of what a member may open is the web app's."""
    caps = access.capabilities_for(
        role, ["chat", "agents", "channels", "routines", "credits", "integrations", "admin"]
    )  # type: ignore[arg-type]
    paged = {link.cap for link in DEEP_LINKS.values()}
    expected = {c for c in paged if c in caps}
    ours = {c for c in paged if role == "admin" or c in MEMBER_CAPS}
    assert ours == expected, f"{role} may open the same pages in bot and web"
