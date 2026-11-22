"""Executable record: chat choices only Discord has, and why Slack and Teams do not.

- `answer_bots`: Slack and Teams drop every message a bot or app sends before any
  setting is read, so there is no bot to answer.
- The `/model` effort menu and the emoji redraw: Slack's and Teams' pickers list
  models only, and their messages carry no model emoji (`model_reactions` is
  Discord's). A pick by the agent's `set_thread_model` tool runs outside the chat
  process, so its emoji catch up on the next turn instead.

If a platform gains one, update this record rather than delete it.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.features.registry import FEATURES

_ADAPTERS = Path(__file__).resolve().parents[2] / "packages/adapters"


def test_answering_bots_and_the_timed_card_are_discord_settings() -> None:
    for key in ("answer_bots", "card_timer", "model_reactions"):
        assert FEATURES[key].platforms == frozenset({"discord"}), key


def test_only_discords_model_picker_offers_an_effort_and_redraws_the_emoji() -> None:
    pickers = {
        "discord": _ADAPTERS / "discord/daimon/adapters/discord/commands/model.py",
        "slack": _ADAPTERS / "slack/daimon/adapters/slack/thread_model.py",
        "teams": _ADAPTERS / "teams/daimon/adapters/teams/thread_model.py",
        "mcp": _ADAPTERS / "mcp/daimon/adapters/mcp/tools/thread_model.py",
    }
    names = ("THREAD_EFFORTS", "redraw_model_marks")
    has = {p: {n for n in names if n in path.read_text()} for p, path in pickers.items()}
    assert has["discord"] == set(names), "the effort menu and the redraw"
    assert not any(has[p] for p in ("slack", "teams", "mcp")), has
