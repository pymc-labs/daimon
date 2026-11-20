"""What each platform would have shown: its message splitting, and Slack and Teams card JSON
from the core builders, for the review view's raw tab."""

from __future__ import annotations

from typing import Final

from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.evals.format import Platform, PlatformPayloads
from daimon.core.message_split import split_fenced
from daimon.core.posted_controls.cards import PostedCard
from daimon.core.posted_controls.confirmation import (
    build_confirmation_blocks,
    build_confirmation_card,
)
from daimon.core.posted_controls.slack_blocks import build_card_blocks
from daimon.core.posted_controls.teams_card import build_adaptive_card, teams_wording
from pydantic import TypeAdapter

#: A message's longest text on each platform, as its adapter posts replies.
LIMITS: Final[dict[Platform, int]] = {"discord": 2000, "slack": 11800, "teams": 28000}
_PAYLOADS: Final = TypeAdapter(PlatformPayloads)


def message_payloads(text: str) -> PlatformPayloads:
    """The chunks a reply posts as on each platform; the web shows it whole."""
    return _PAYLOADS.validate_python(
        {p: {"messages": split_fenced(text, limit)} for p, limit in LIMITS.items()}
    )


def confirmation_payloads(card_id: str, prompt: ConfirmationPrompt) -> PlatformPayloads:
    card = build_confirmation_card(prompt, state="pending", token=card_id)
    return _PAYLOADS.validate_python(
        {"slack": {"blocks": build_confirmation_blocks(card, prompt=prompt)}}
    )


def posted_payloads(card: PostedCard) -> PlatformPayloads:
    return _PAYLOADS.validate_python(
        {
            "slack": {"blocks": build_card_blocks(card)},
            "teams": build_adaptive_card(teams_wording(card)),
        }
    )
