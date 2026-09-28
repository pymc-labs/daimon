"""Platform-neutral confirmation card: the copy, its states, and Block Kit.

One card per `ConfirmationPrompt`. It is posted `pending` with Approve and
Deny, then edited in place to the answer. Discord and Slack draw the same
`ConfirmationCard`; the words and the state machine live only here, the same
split `cards.PostedCard` uses for the credential cards.

Button ids are `dcf:<token>:approve` / `dcf:<token>:deny`; the token is the
one `daimon.core.confirmation.PendingConfirmations.open` handed out.

Pure module — no I/O, no clock.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Final, Literal

from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from pydantic import BaseModel, ConfigDict

__all__ = [
    "CONFIRMATION_ACTION_PATTERN",
    "CONFIRMATION_CUSTOM_ID_PREFIX",
    "ConfirmationCard",
    "ConfirmationCardState",
    "NOT_YOURS_MESSAGE",
    "NO_LONGER_PENDING_MESSAGE",
    "build_confirmation_blocks",
    "build_confirmation_card",
    "confirmation_card_text",
    "confirmation_custom_id",
    "parse_confirmation_custom_id",
]

ConfirmationCardState = Literal["pending", "approved", "denied", "expired"]
ConfirmationChoice = Literal["approve", "deny"]

CONFIRMATION_CUSTOM_ID_PREFIX: Final[str] = "dcf:"
CONFIRMATION_ACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^dcf:(?P<token>[A-Za-z0-9_-]{8,64}):(?P<choice>approve|deny)$"
)

NOT_YOURS_MESSAGE: Final[str] = "Only the person who asked can answer this."
NO_LONGER_PENDING_MESSAGE: Final[str] = "This was already answered or has expired."

_HEADLINE_MARK: Final[dict[ConfirmationCardState, str]] = {
    "pending": "✋",
    "approved": "✅",
    "denied": "🛡️",
    "expired": "⌛",
}


class ConfirmationCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ConfirmationCardState
    headline: str
    fields: tuple[tuple[str, str], ...]
    detail: str | None
    footer: str | None
    #: Present only while `pending`.
    token: str | None = None


def confirmation_custom_id(token: str, choice: ConfirmationChoice) -> str:
    return f"{CONFIRMATION_CUSTOM_ID_PREFIX}{token}:{choice}"


def parse_confirmation_custom_id(custom_id: str) -> tuple[str, ConfirmationAnswer] | None:
    """`(token, answer)` for a confirmation button id, else `None`."""
    match = CONFIRMATION_ACTION_PATTERN.match(custom_id)
    if match is None:
        return None
    answer: ConfirmationAnswer = "approved" if match["choice"] == "approve" else "denied"
    return match["token"], answer


def build_confirmation_card(
    prompt: ConfirmationPrompt,
    *,
    state: ConfirmationCardState,
    token: str | None = None,
    answered_by_platform_user_id: str | None = None,
) -> ConfirmationCard:
    """The card for `prompt` in `state`.

    `token` is required for `pending` (the buttons carry it) and refused
    otherwise, so an answered card can never be drawn with live buttons.
    """
    if (token is not None) != (state == "pending"):
        raise ValueError("token belongs to state='pending' only")
    if state == "pending":
        headline = f"{_HEADLINE_MARK[state]} {prompt.title}"
        footer = "Only {requester} can answer. Expires {expires}."
    elif state == "approved":
        who = answered_by_platform_user_id or prompt.requester_platform_user_id
        headline = f"{_HEADLINE_MARK[state]} Approved — running it."
        footer = f"Approved by <@{who}>."
    elif state == "denied":
        headline = f"{_HEADLINE_MARK[state]} Denied — it did not run."
        footer = None
    else:
        headline = f"{_HEADLINE_MARK[state]} No answer in time — it did not run."
        footer = None
    return ConfirmationCard(
        state=state,
        headline=headline,
        fields=prompt.fields,
        detail=prompt.detail,
        footer=footer,
        token=token,
    )


def _slack_expires(expires_at: datetime) -> str:
    unix = int(expires_at.timestamp())
    fallback = datetime.fromtimestamp(unix, UTC).strftime("%H:%M")
    return f"<!date^{unix}^{{time}}|{fallback} UTC>"


def build_confirmation_blocks(
    card: ConfirmationCard, *, prompt: ConfirmationPrompt
) -> list[dict[str, Any]]:
    """Render `card` as Slack Block Kit, buttons only while pending."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{card.headline}*"}},
    ]
    if card.fields:
        field_elements: list[dict[str, Any]] = [
            {"type": "mrkdwn", "text": f"*{label}*\n`{value}`"} for label, value in card.fields
        ]
        blocks.append({"type": "section", "fields": field_elements})
    if card.detail:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{card.detail}```"}}
        )
    if card.token is not None:
        buttons: list[dict[str, Any]] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "action_id": confirmation_custom_id(card.token, "approve"),
                "value": card.token,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Deny"},
                "style": "danger",
                "action_id": confirmation_custom_id(card.token, "deny"),
                "value": card.token,
            },
        ]
        blocks.append({"type": "actions", "elements": buttons})
    if card.footer is not None:
        footer = card.footer.format(
            requester=f"<@{prompt.requester_platform_user_id}>",
            expires=_slack_expires(prompt.expires_at),
        )
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def confirmation_card_text(card: ConfirmationCard) -> str:
    """Plain-text fallback (notifications, screen readers, tests)."""
    lines = [card.headline, *(f"{label}: {value}" for label, value in card.fields)]
    return "\n".join(lines)
