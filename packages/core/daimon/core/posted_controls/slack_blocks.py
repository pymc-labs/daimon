"""Block Kit rendering of a `PostedCard`.

Slack's surface is a plain JSON dict API, so this renderer lives in core next
to the card model rather than in the Slack adapter: the MCP server posts the
first card and the Slack bot edits it after the form, and those two processes
may not import each other. Nothing here imports `slack_sdk` — the blocks are
literal dicts, exactly as the Web API takes them.

`dict[str, Any]` is the honest type for a Block Kit element: the schema is a
union of ~30 block shapes whose fields differ, and `slack_sdk` models them as
untyped dicts on the wire. Bindings are annotated explicitly so the `Any` stops
at this module's boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from daimon.core.credential_requests import SLACK_ACTION_ID
from daimon.core.posted_controls.cards import PostedCard

__all__ = ["MAX_CONTEXT_ELEMENTS", "build_card_blocks", "card_notification_text"]

#: Slack's documented cap on `context` block elements.
MAX_CONTEXT_ELEMENTS: Final[int] = 10


def _mrkdwn(text: str) -> dict[str, Any]:
    element: dict[str, Any] = {"type": "mrkdwn", "text": text}
    return element


def _context(lines: tuple[str, ...]) -> dict[str, Any]:
    # More facts than Slack allows elements: fold them into one element rather
    # than dropping lines. A line-numbered .env refusal can be long.
    elements = (
        [_mrkdwn(line) for line in lines]
        if len(lines) <= MAX_CONTEXT_ELEMENTS
        else [_mrkdwn("\n".join(lines))]
    )
    block: dict[str, Any] = {"type": "context", "elements": elements}
    return block


def _slack_expires(expires_at_unix: int) -> str:
    """Slack's live-timestamp token, with the UTC fallback it shows on failure."""
    fallback = datetime.fromtimestamp(expires_at_unix, UTC).strftime("%H:%M")
    return f"<!date^{expires_at_unix}^{{time}}|{fallback} UTC>"


def _requested_footer(card: PostedCard) -> str:
    if card.footer is None or card.requester_platform_user_id is None:
        raise ValueError("a requested card must carry a footer, a requester and an expiry")
    if card.expires_at_unix is None:
        raise ValueError("a requested card must carry an expiry")
    return card.footer.format(
        requester=f"<@{card.requester_platform_user_id}>",
        expires=_slack_expires(card.expires_at_unix),
    )


def build_card_blocks(card: PostedCard, *, token: str | None = None) -> list[dict[str, Any]]:
    """Render one posted card as Block Kit blocks.

    `token` is the request token the private-form button carries in its
    `value`; Slack routes the click by `action_id` and hands the value back.
    It is required whenever the card has a form button, which is only ever the
    `requested` state.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": _mrkdwn(f"*{card.headline}*")},
    ]
    if card.facts:
        blocks.append(_context(card.facts))
    if card.buttons:
        elements: list[dict[str, Any]] = []
        for index, button in enumerate(card.buttons):
            element: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": button.label, "emoji": True},
            }
            if button.url is not None:
                element["url"] = button.url
                element["action_id"] = f"{SLACK_ACTION_ID}_link_{index}"
            else:
                if token is None:
                    raise ValueError("a card with a private-form button needs its token")
                element["action_id"] = SLACK_ACTION_ID
                element["value"] = token
                element["style"] = "primary"
            elements.append(element)
        blocks.append({"type": "actions", "elements": elements})
    if card.state == "requested":
        blocks.append(_context((_requested_footer(card),)))
    elif card.footer is not None:
        blocks.append(_context((card.footer,)))
    return blocks


def card_notification_text(card: PostedCard) -> str:
    """The `text` fallback Slack shows in notifications and unsupported clients."""
    return card.headline
