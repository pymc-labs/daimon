"""Adaptive Card rendering of a posted card, for Teams."""

from __future__ import annotations

import json

import pytest
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG, build_adaptive_card

from .test_cards import AGENT, KINDS, STATES, TOKEN, build


def test_requested_card_opens_the_dialog_with_the_token_and_a_local_time_footer() -> None:
    card = build_adaptive_card(build("env", "requested"), token=TOKEN)
    body = card["body"]
    assert isinstance(body, list)
    headline, *facts, actions, footer = body
    assert headline["text"] == f"🔑 {AGENT} needs TOGGL_TOKEN", "a TextBlock shows no backticks"
    assert len(facts) == 1, "each fact is its own line"
    assert [b.get("spacing") for b in (*facts, actions, footer)] == ["Medium"] * 3, (
        "title, body, buttons and footer each sit a blank line apart"
    )
    assert footer["text"].startswith("Only the person who asked can fill this in. Expires ")
    (button,) = actions["actions"]
    assert button["type"] == "Action.Submit"
    assert button["data"] == {
        "msteams": {"type": "task/fetch"},
        "dialog_id": CREDENTIAL_DIALOG,
        "token": TOKEN,
    }, "the button opens the dialog and carries only the opaque request token"
    assert "{{TIME(2026-09-14T17:30:00Z)}}" in footer["text"], "Teams renders it in local time"
    assert "the person who asked" in footer["text"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", [s for s in STATES if s != "requested"])
def test_every_later_state_renders_without_a_form_button(kind: str, state: str) -> None:
    card = json.dumps(build_adaptive_card(build(kind, state)))  # type: ignore[arg-type]
    assert "task/fetch" not in card and TOKEN not in card, "a spent card offers no form"


def test_a_form_button_without_its_token_is_refused() -> None:
    with pytest.raises(ValueError, match="token"):
        build_adaptive_card(build("mcp", "requested"))


def test_an_env_file_card_asks_for_a_paste_on_teams() -> None:
    """A dialog has no file input, so the Teams card never says upload."""
    card = json.dumps(build_adaptive_card(build("env_file", "requested"), token=TOKEN))
    assert "Paste it privately" in card, "the button names what the dialog takes"
    assert "upload" not in card.lower(), "a core copy change must be mirrored in teams_card"
