"""Adaptive Card rendering of a `PostedCard`, for Microsoft Teams.

In core beside `slack_blocks` for the same reason: the MCP process posts the
first card and the Teams adapter edits it, and neither may import the other.
Plain JSON dicts, exactly as the Bot Framework takes them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from daimon.core.posted_controls.cards import PostedCard, card_for_request

__all__ = [
    "ADAPTIVE_CARD_TYPE",
    "CREDENTIAL_DIALOG",
    "build_adaptive_card",
    "card_for_request",
]

ADAPTIVE_CARD_TYPE: Final[str] = "application/vnd.microsoft.card.adaptive"
#: `dialog_id` of the private form the card's button opens through `task/fetch`.
CREDENTIAL_DIALOG: Final[str] = "credential_request"
# The poster knows the requester's Entra id, not their name, so the footer
# names the role; only the requester can open the form either way.
_REQUESTER: Final[str] = "the person who asked"


def _text(text: str, **style: object) -> dict[str, object]:
    return {"type": "TextBlock", "text": text, "wrap": True, **style}


def _expires(expires_at_unix: int) -> str:
    """Teams renders `{{TIME(...)}}` in the reader's own time zone."""
    stamp = datetime.fromtimestamp(expires_at_unix, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{{{{TIME({stamp})}}}}"


def _footer(card: PostedCard) -> str | None:
    if card.footer is None or card.expires_at_unix is None:
        return card.footer
    return card.footer.format(requester=_REQUESTER, expires=_expires(card.expires_at_unix))


def build_adaptive_card(card: PostedCard, *, token: str | None = None) -> dict[str, object]:
    """Render one posted card; `token` rides in the form button's `task/fetch` data.

    The token is the opaque request handle, never a secret value, and is
    required whenever the card has a form button (only the `requested` state).
    """
    body: list[dict[str, object]] = [_text(card.headline, weight="Bolder")]
    body += [_text(fact, isSubtle=True, spacing="None") for fact in card.facts]
    actions: list[dict[str, object]] = []
    for button in card.buttons:
        if button.url is not None:
            actions.append({"type": "Action.OpenUrl", "title": button.label, "url": button.url})
            continue
        if token is None:
            raise ValueError("a card with a private-form button needs its token")
        data = {"msteams": {"type": "task/fetch"}, "dialog_id": CREDENTIAL_DIALOG, "token": token}
        actions.append({"type": "Action.Submit", "title": button.label, "data": data})
    if actions:
        body.append({"type": "ActionSet", "actions": actions})
    footer = _footer(card)
    if footer is not None:
        body.append(_text(footer, isSubtle=True, size="Small"))
    return {"type": "AdaptiveCard", "version": "1.5", "fallbackText": card.headline, "body": body}
