"""The web refusal cards for a thread the provider no longer holds."""

from __future__ import annotations

from daimon.adapters.web.refusals import refusal_for
from daimon.core.turn.errors import AdmissionDenied


def test_a_lost_conversation_card_says_to_start_a_new_thread() -> None:
    card = refusal_for(AdmissionDenied(reason="backend_unsupported", cause="session_lost"))
    assert "new thread" in card.help, "the card tells the person what to do"
