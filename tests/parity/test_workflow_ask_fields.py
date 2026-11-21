"""A question's typed fields ask the same way on every chat platform: a pick for a choice,
a text box for the rest, optional where the agent left them optional."""

from __future__ import annotations

from typing import Any

import discord
from daimon.adapters.discord.workflow_asks import AskModal
from daimon.adapters.slack.workflow_asks import _form_view  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.teams.workflow_asks import ask_card
from daimon.core.workflows import answers, waits

ASK: dict[str, Any] = {
    "wait_id": "00000000-0000-0000-0000-000000000001",
    "title": "Where?",
    "shape": "form",
    "routes": [{"name": "answer", "label": "Answer"}],
    "fields": [
        {"name": "channel", "title": "Channel", "choices": ["#delivery", "#leads"]},
        {"name": "note", "title": "Note", "choices": [], "required": False},
    ],
}


async def test_a_choice_field_is_a_pick_and_a_note_a_text_box_everywhere() -> None:
    form = answers.form_of(ASK, waits.ANSWER)
    assert form is not None, "a form ask opens its fields"

    modal = AskModal(form, route=waits.ANSWER, message_id="1")
    [label, note] = modal.children
    assert isinstance(label, discord.ui.Label) and isinstance(label.component, discord.ui.Select)
    picks = [o.label for o in label.component.options]
    assert (label.text, picks) == ("Channel", ["#delivery", "#leads"]), "Discord: a select"
    assert isinstance(note, discord.ui.TextInput) and not note.required, "Discord: an optional box"

    blocks = _form_view(form, {})["blocks"]
    assert [(b["element"]["type"], b["optional"]) for b in blocks] == [
        ("static_select", False),
        ("plain_text_input", True),
    ], f"Slack: a select, then an optional box: {blocks}"

    body = ask_card("Where?", ASK).body
    kinds = [type(e).__name__ for e in body if type(e).__name__.endswith("Input")]
    assert kinds == ["ChoiceSetInput", "TextInput"], f"Teams: a choice set, then a box: {kinds}"
