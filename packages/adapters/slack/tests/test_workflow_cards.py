"""Workflow cards on Slack: the confirmation reads back as shown and the run card is edited."""

from __future__ import annotations

import uuid
from typing import Any

from daimon.adapters.slack import workflow_cards
from daimon.core.workflows import chat_cards
from daimon.testing.slack_surface import SlackSurfaceHarness


async def test_a_posted_confirmation_reads_back_and_the_card_is_edited_in_place() -> None:
    harness = SlackSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    chat = workflow_cards._Chat(harness.client, "C_QA", "U_QA", "1699999999.000001")  # pyright: ignore[reportPrivateUsage]
    inputs = {"email": "jane@example.com", "note": "a & b"}
    place = await chat.post(chat_cards.confirm_card(uuid.uuid4(), "Lead", 1, inputs, None))
    posted = harness.recorder.payload(place.message_id)
    keys = frozenset(inputs)
    assert chat_cards.confirmed_inputs(workflow_cards.card_text(posted["blocks"]), keys) == inputs
    await chat.edit(place, chat_cards.starting_card("Lead"))
    edited = harness.recorder.payload(place.message_id)
    assert "Lead" in edited["text"] and "go:" not in str(edited["blocks"])


def test_the_run_form_names_its_form_and_where_the_run_card_goes() -> None:
    spec: dict[str, Any] = {
        "name": "Lead",
        "inputs": {"required": ["email"], "properties": {"email": {}}},
    }
    form = chat_cards.run_form(uuid.uuid4(), spec, limit=workflow_cards.FORM_LIMIT)
    assert form is not None
    view = workflow_cards.form_view(form, "C1", None)
    assert view["callback_id"] == workflow_cards.FORM_CALLBACK_ID
    assert [b["block_id"] for b in view["blocks"]] == ["email"]
    assert form.submit_id in view["private_metadata"]
