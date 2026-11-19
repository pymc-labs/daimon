"""The web refusal cards for a thread the provider no longer holds."""

from __future__ import annotations

import pytest
from daimon.adapters.web.refusals import refusal_for
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_lines


def test_a_lost_conversation_card_says_to_start_a_new_thread() -> None:
    card = refusal_for(AdmissionDenied(reason="backend_unsupported", cause="session_lost"))
    assert "new thread" in card.help, "the card tells the person what to do"


@pytest.mark.parametrize(
    ("reason", "provider"),
    [("provider_daily_ceiling_reached", "openai"), ("provider_daily_cap_reached", "gpt-6-astra")],
)
def test_a_spent_daily_budget_card_says_what_chat_says(
    reason: AdmissionDenialReason, provider: str
) -> None:
    card = refusal_for(AdmissionDenied(reason=reason, provider=provider))
    title, help = admission_refusal_lines(reason, RefusalNouns.for_caller("web"), provider=provider)
    assert (card.title, card.help) == (title, help), "the web words are the shared notice's words"
    assert "midnight UTC" in card.help, "the card says when it resets"
