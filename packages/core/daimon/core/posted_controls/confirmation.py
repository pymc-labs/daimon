"""Shared approval card words, states and Slack Block Kit rendering."""

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
    "EXPIRED_MESSAGE",
    "build_confirmation_blocks",
    "build_confirmation_card",
    "confirmation_card_text",
    "confirmation_custom_id",
    "parse_confirmation_custom_id",
]

ConfirmationCardState = Literal["pending", "approved", "denied", "expired", "stopped"]
ConfirmationChoice = Literal["approve", "deny", "details"]
CONFIRMATION_CUSTOM_ID_PREFIX: Final[str] = "dcf:"
CONFIRMATION_ACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^dcf:(?P<token>[A-Za-z0-9_-]{8,64}):(?P<choice>approve|deny|details)$"
)
NOT_YOURS_MESSAGE: Final[str] = "Only {requester} can approve or deny this request."
NO_LONGER_PENDING_MESSAGE: Final[str] = "This request was already answered."
EXPIRED_MESSAGE: Final[str] = "This request expired."


class ConfirmationCard(BaseModel):
    model_config = ConfigDict(frozen=True)
    state: ConfirmationCardState
    headline: str
    detail_lines: tuple[str, ...] = ()
    consequence: str | None = None
    body: str | None = None
    items: tuple[str, ...] = ()
    footer: str | None
    token: str | None = None


def confirmation_custom_id(token: str, choice: ConfirmationChoice) -> str:
    return f"{CONFIRMATION_CUSTOM_ID_PREFIX}{token}:{choice}"


def parse_confirmation_custom_id(custom_id: str) -> tuple[str, ConfirmationAnswer] | None:
    match = CONFIRMATION_ACTION_PATTERN.match(custom_id)
    if match is None or match["choice"] == "details":
        return None
    return match["token"], "approved" if match["choice"] == "approve" else "denied"


def parse_details_custom_id(custom_id: str) -> str | None:
    match = CONFIRMATION_ACTION_PATTERN.match(custom_id)
    return match["token"] if match is not None and match["choice"] == "details" else None


def build_confirmation_card(
    prompt: ConfirmationPrompt,
    *,
    state: ConfirmationCardState,
    token: str | None = None,
    answered_by_platform_user_id: str | None = None,
) -> ConfirmationCard:
    if (token is not None) != (state == "pending"):
        raise ValueError("token belongs to state='pending' only")
    if state == "pending":
        headline = prompt.title
        body = None
        footer = "Only {requester} can approve or deny\nExpires {expires}"
    else:
        headline = state.capitalize()
        body = (
            prompt.action or prompt.title.removesuffix("?")
            if state == "approved"
            else prompt.denied_action or f"{prompt.title.removesuffix('?')} not completed"
        )
        footer = "by {requester}" if state in {"approved", "denied"} else None
    return ConfirmationCard(
        state=state,
        headline=headline,
        body=body,
        consequence=prompt.consequence if state == "pending" else None,
        detail_lines=prompt.detail_lines if state == "pending" else (),
        items=prompt.items if state == "pending" else (),
        footer=footer,
        token=token,
    )


def _slack_expires(expires_at: datetime) -> str:
    unix = int(expires_at.timestamp())
    fallback = datetime.fromtimestamp(unix, UTC).strftime("%H:%M")
    return f"<!date^{unix}^{{ago}}|{fallback} UTC>"


def build_confirmation_blocks(
    card: ConfirmationCard, *, prompt: ConfirmationPrompt
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": card.headline[:150]}},
    ]
    if card.body:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": card.body}})
    if card.items:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": "\n".join(f"• {item}" for item in card.items),
                },
            }
        )
    if card.consequence:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": card.consequence}})
    if card.token is not None:
        blocks.append({"type": "divider"})
        count = len(card.items)
        buttons = [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": f"Approve all {count}" if count else "Approve",
                },
                "style": "primary",
                "action_id": confirmation_custom_id(card.token, "approve"),
                "value": card.token,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"Deny all {count}" if count else "Deny"},
                "style": "danger",
                "action_id": confirmation_custom_id(card.token, "deny"),
                "value": card.token,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Details"},
                "action_id": confirmation_custom_id(card.token, "details"),
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
    return "\n".join(
        part for part in (card.headline, card.body, *card.items, card.consequence) if part
    )
