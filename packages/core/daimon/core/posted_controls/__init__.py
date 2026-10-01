"""Platform-neutral model and renderers for the posted control cards.

`cards` owns the copy and the state machine; `slack_blocks` draws one card as
Block Kit. The Discord renderer lives in its adapter, because it needs
`discord.ui` types this package must not import.
"""

from __future__ import annotations

from daimon.core.posted_controls.cards import (
    ALREADY_USED_MESSAGE,
    EXPIRED_HEADLINE,
    FOOTER_TEMPLATE,
    NO_LONGER_VALID_MESSAGE,
    RECEIVED_FOOTER,
    REPLACED_HEADLINE,
    WRONG_REQUESTER_MESSAGE,
    CardButton,
    CardKind,
    CardState,
    PostedCard,
    RefusalReason,
    build_posted_card,
    card_for_request,
    card_text,
    classify_card_state,
    expired_message,
)
from daimon.core.posted_controls.slack_blocks import build_card_blocks, card_notification_text

__all__ = [
    "ALREADY_USED_MESSAGE",
    "EXPIRED_HEADLINE",
    "FOOTER_TEMPLATE",
    "NO_LONGER_VALID_MESSAGE",
    "RECEIVED_FOOTER",
    "REPLACED_HEADLINE",
    "WRONG_REQUESTER_MESSAGE",
    "CardButton",
    "CardKind",
    "CardState",
    "PostedCard",
    "RefusalReason",
    "build_card_blocks",
    "build_posted_card",
    "card_for_request",
    "card_notification_text",
    "card_text",
    "classify_card_state",
    "expired_message",
]
