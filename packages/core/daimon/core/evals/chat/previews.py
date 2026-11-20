"""What each platform would have shown: an answer in its markdown dialect and message size,
and Slack and Teams card JSON from the core builders, for the review view's raw tab."""

from __future__ import annotations

from typing import Final

from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.evals.format import PlatformPayloads
from daimon.core.message_split import split_fenced
from daimon.core.posted_controls.cards import PostedCard
from daimon.core.posted_controls.confirmation import (
    build_confirmation_blocks,
    build_confirmation_card,
)
from daimon.core.posted_controls.slack_blocks import build_card_blocks
from daimon.core.posted_controls.teams_card import build_adaptive_card, teams_wording
from daimon.core.slack_mrkdwn import SLACK_LIMIT, answer_chunks
from daimon.core.teams_markdown import escape_dollars
from pydantic import TypeAdapter

#: A message's longest text on each platform, as its adapter posts replies (Slack's is
#: `SLACK_LIMIT`).
DISCORD_LIMIT: Final = 1900
TEAMS_LIMIT: Final = 4000
_PAYLOADS: Final = TypeAdapter(PlatformPayloads)


def message_payloads(text: str) -> PlatformPayloads:
    """An answer as each platform posts it. Discord renders markdown as written. Slack gets
    native markdown blocks, entities escaped outside code. Teams gets Extended Markdown, its
    dollar signs escaped so prices are not read as math. The web shows the answer whole."""
    slack = answer_chunks(text, limit=SLACK_LIMIT)
    return _PAYLOADS.validate_python(
        {
            "discord": {"messages": split_fenced(text, DISCORD_LIMIT)},
            "slack": {
                "messages": slack,
                "blocks": [{"type": "markdown", "text": chunk} for chunk in slack],
            },
            "teams": {
                "messages": [escape_dollars(c) for c in split_fenced(text, TEAMS_LIMIT)],
                "text_format": "extendedmarkdown",
            },
        }
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
