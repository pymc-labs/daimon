"""Workflow cards on Discord: the confirmation reads back as shown and the follower edits."""

from __future__ import annotations

import uuid

from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.workflow_cards import card_editor, card_text
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace
from daimon.testing.discord_surface import DiscordSurfaceHarness


async def test_a_posted_confirmation_reads_back_and_the_card_is_edited_in_place() -> None:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    thread = harness.thread
    inputs = {"email": "jane@example.com", "n": 3}
    card = chat_cards.confirm_card(uuid.uuid4(), "Lead", 1, inputs, None)
    sent = await thread.send(view=build_card_view(card))
    assert chat_cards.confirmed_inputs(card_text(sent.components), frozenset(inputs)) == inputs

    async def fetch(channel_id: int) -> object:
        assert channel_id == thread.id, "the card's own channel"
        return thread

    place = ChatPlace("discord", str(thread.id), str(sent.id))
    await card_editor(fetch, place, chat_cards.starting_card("Lead"))
    [shown] = harness.gateway.surviving_messages()
    assert "Lead" in shown.payload_json and "Run" not in shown.component_labels
