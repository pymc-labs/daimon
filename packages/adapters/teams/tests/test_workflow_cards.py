"""Workflow cards on Teams: the form and panel cards, and the run card edited in place."""

from __future__ import annotations

import uuid
from typing import Any

from daimon.adapters.teams import workflow_cards
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace, RunForm
from daimon.testing.teams_surface import TeamsSurfaceHarness
from microsoft_teams.api import MessageActivityInput


async def test_the_follower_edits_the_run_card_in_its_conversation() -> None:
    harness = TeamsSurfaceHarness(
        evidence_id="wf", session_id="s", root_turn_id="r", service_url=SERVICE_URL
    )
    first = MessageActivityInput().add_card(workflow_cards.form_card(_form()))
    sent = await harness.sender.send("19:qa-thread", first, service_url=SERVICE_URL)
    edit = workflow_cards.card_editor(harness.sender)
    await edit(
        uuid.uuid4(), ChatPlace("teams", "19:qa-thread", sent.id), chat_cards.starting_card("Lead")
    )
    [shown] = harness.recorder.surviving_messages()
    assert shown.message_id == sent.id and "starting" in shown.payload_json


def _form() -> RunForm:
    spec: dict[str, Any] = {"name": "Lead", "inputs": {"properties": {"email": {}, "n": {}}}}
    form = chat_cards.run_form(uuid.uuid4(), spec, limit=workflow_cards.FORM_LIMIT)
    assert form is not None
    return form


def test_the_run_form_submits_its_own_id_with_each_field() -> None:
    form = _form()
    card = workflow_cards.form_card(form).model_dump(by_alias=True, exclude_none=True)
    assert [b.get("id") for b in card["body"][1:]] == ["email", "n"]
    assert card["actions"][0]["data"]["custom_id"] == form.submit_id
