"""Executable record: the "Waiting for the AI service" headline, the ended label and the
asker ping on a stop or error card are Discord's alone.

Slack and Teams cards show "Working on it" throughout and end without either; they have
no port yet. If one gains it, update this record rather than delete it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "packages/adapters"
_CARDS = {
    "discord": _ROOT / "discord/daimon/adapters/discord/embed.py",
    "slack": _ROOT / "slack/daimon/adapters/slack/blockkit.py",
    "teams": _ROOT / "teams/daimon/adapters/teams/card.py",
}


def _headline_keywords(path: Path) -> set[str]:
    calls = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "format_headline"
    ]
    assert calls, path
    return {kw.arg for call in calls for kw in call.keywords if kw.arg}


def test_only_discord_shows_the_waiting_headline() -> None:
    assert "waiting" in _headline_keywords(_CARDS["discord"])
    assert all("waiting" not in _headline_keywords(_CARDS[p]) for p in ("slack", "teams"))


def test_only_discord_labels_the_end_and_pings_the_asker() -> None:
    users = {
        path.relative_to(_ROOT).parts[0]
        for path in _ROOT.glob("*/daimon/**/*.py")
        if "ended_label" in path.read_text() or "_asker_to_mention" in path.read_text()
    }
    assert users == {"discord"}


def test_only_discord_times_the_headline() -> None:
    """`card_timer` is Discord's: Slack and Teams keep "Working on it" with no time."""
    assert "timed" in _headline_keywords(_CARDS["discord"])
    assert all("timed" not in _headline_keywords(_CARDS[p]) for p in ("slack", "teams"))
